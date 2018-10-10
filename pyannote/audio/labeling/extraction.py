#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2016-2018 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

import numpy as np
from cachetools import LRUCache
CACHE_MAXSIZE = 12

import torch
from pyannote.core import SlidingWindow, SlidingWindowFeature
from pyannote.generators.batch import FileBasedBatchGenerator
from pyannote.generators.fragment import SlidingSegments
from pyannote.database import get_unique_identifier
from pyannote.audio.features import Precomputed


class SequenceLabeling(FileBasedBatchGenerator):
    """Sequence labeling

    Parameters
    ----------
    model : nn.Module
        Pre-trained sequence labeling model.
    feature_extraction : pyannote.audio.features.Precomputed
        Feature extractor
    duration : float, optional
        Subsequence duration, in seconds. Defaults to 1s.
    min_duration : float, optional
        When provided, will do its best to yield segments of length `duration`,
        but shortest segments are also permitted (as long as they are longer
        than `min_duration`).
    step : float, optional
        Subsequence step, in seconds. Defaults to 50% of `duration`.
    batch_size : int, optional
        Defaults to 32.
    device : torch.device, optional
        Defaults to CPU.

    Example
    -------
    >>> labeler = SequenceLabeling(model, feature_extraction)
    >>> predictions = labeler.apply(current_file)
    """

    def __init__(self, model, feature_extraction, duration=1,
                 min_duration=None, step=None, batch_size=32, source='audio',
                 device=None):

        self.feature_extraction = feature_extraction
        self.duration = duration
        self.min_duration = min_duration
        self.device = torch.device('cpu') if device is None else device
        self.model = model.eval().to(self.device)

        generator = SlidingSegments(duration=duration, step=step,
                                    min_duration=min_duration, source=source)
        self.step = generator.step if step is None else step

        super(SequenceLabeling, self).__init__(
            generator, {'@': (self._process, self._pack)},
            batch_size=batch_size, incomplete=False)

    @property
    def dimension(self):
        if hasattr(self.model, 'n_classes'):
            return self.model.n_classes
        elif hasattr(self.model, 'output_dim'):
            return self.model.output_dim
        else:
            raise ValueError('Model has no n_classes nor output_dim attribute.')

    @property
    def sliding_window(self):
        return self.feature_extraction.sliding_window

    def preprocess(self, current_file):
        """On-demand feature extraction

        Parameters
        ----------
        current_file : dict
            Generated by a pyannote.database.Protocol

        Returns
        -------
        current_file : dict
            Current file with additional "features" entry

        Notes
        -----
        Does nothing when self.feature_extraction is a
        pyannote.audio.features.Precomputed instance.
        """

        # if "features" are precomputed on disk, do nothing
        # as "process_segment" will load just the part we need
        if isinstance(self.feature_extraction, Precomputed):
            return current_file

        # if (by chance) current_file already contains "features"
        # do nothing.
        if 'features' in current_file:
            return current_file

        # if we get there, it means that we need to extract features
        # for current_file. let's create a cache to store them...
        if not hasattr(self, 'preprocessed_'):
            self.preprocessed_ = LRUCache(maxsize=CACHE_MAXSIZE)

        # this is the key that will be used to know if "features"
        # already exist in cache
        uri = get_unique_identifier(current_file)

        # if "features" are not cached for current file
        # compute and cache them...
        if uri not in self.preprocessed_:
            features = self.feature_extraction(current_file)
            self.preprocessed_[uri] = features

        # create copy of current_file to prevent "features"
        # from consuming increasing memory...
        preprocessed = dict(current_file)

        # add "features" key
        preprocessed['features'] = self.preprocessed_[uri]

        return preprocessed

    def _process(self, segment, current_file=None):
        """Extract features for current segment

        Parameters
        ----------
        segment : pyannote.core.Segment
        current_file : dict
            Generated by a pyannote.database.Protocol
        """

        # use in-memory "features" whenever they are available
        if 'features' in current_file:
            features = current_file['features']
            return features.crop(segment, mode='center', fixed=self.duration,
                                 return_data=True)

        # this line will only happen when self.feature_extraction is a
        # pyannote.audio.features.Precomputed instance
        return self.feature_extraction.crop(current_file, segment,
                                            mode='center', fixed=self.duration,
                                            return_data=True)

    def _pack(self, sequences):
        """

        Parameters
        ----------
        sequences : list
            List of `batch_size` numpy array of shape (n_samples, n_features)

        Returns
        -------
        prediction : (batch_size, n_samples, n_scores) numpy array
            Predictions
        """

        X = torch.tensor(np.stack(sequences), dtype=torch.float32,
                         device=self.device)
        return self.model(X).data.to('cpu').numpy()

    def apply(self, current_file):
        """Compute predictions on a sliding window

        Parameter
        ---------
        current_file : dict

        Returns
        -------
        predictions : SlidingWindowFeature
        """

        # frame and sub-sequence sliding windows
        frames = self.feature_extraction.sliding_window
        batches = [batch for batch in self.from_file(current_file,
                                                     incomplete=True)]
        if not batches:
            data = np.zeros((0, self.dimension), dtype=np.float32)
            return SlidingWindowFeature(data, frames)

        fX = np.vstack(batches)

        subsequences = SlidingWindow(duration=self.duration, step=self.step)

        # get total number of frames
        if isinstance(self.feature_extraction, Precomputed):
            n_frames, _ = self.feature_extraction.shape(current_file)
        else:
            uri = get_unique_identifier(current_file)
            n_frames, _ = self.preprocessed_[uri].data.shape

        # data[i] is the sum of all predictions for frame #i
        data = np.zeros((n_frames, self.dimension), dtype=np.float32)

        # k[i] is the number of sequences that overlap with frame #i
        k = np.zeros((n_frames, 1), dtype=np.int8)

        for subsequence, fX_ in zip(subsequences, fX):

            # indices of frames overlapped by subsequence
            indices = frames.crop(subsequence,
                                  mode='center',
                                  fixed=self.duration)

            # accumulate the outputs
            data[indices] += fX_

            # keep track of the number of overlapping sequence
            # TODO - use smarter weights (e.g. Hamming window)
            k[indices] += 1

        # compute average embedding of each frame
        data = data / np.maximum(k, 1)

        return SlidingWindowFeature(data, frames)
