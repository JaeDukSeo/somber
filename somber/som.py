import logging
import time

import json
import cupy as cp
import numpy as np

from tqdm import tqdm
from .utils import linear, expo, np_min, resize
from functools import reduce
from collections import Counter, defaultdict
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler


logger = logging.getLogger(__name__)


class Scaler(object):
    """Simple scaler based on mean and stdev."""

    def __init__(self):
        """
        Scales data based on the mean and standard deviation.

        Reimplemented because this needs to deal with both numpy and cupy
        arrays.
        """
        self.mean = 0
        self.std = 0
        self.is_fit = False

    def fit_transform(self, X):
        """
        Fit and transform the scaler based on some data.

        :param X: The input data as an array.
        :return: A scaled version of said array.
        """
        self.fit(X)
        return self.transform(X)

    def fit(self, X):
        """
        Fit the scaler based on some data.

        Gets the mean  and standard deviation of all feature values
        and stores them.

        :param X: The input data as an array.
        """
        self.mean = X.mean(0)
        self.std = X.std(0)
        self.is_fit = True

    def transform(self, X):
        """
        Transform some data by rescaling it.

        First subtracts the mean from the data, and then divides by the
        standard deviation.

        :param X: The input data as an array.
        :return: A scaled version of the array.
        """
        return (X-self.mean) / (self.std + 10e-6)

    def inverse_transform(self, X):
        """
        Revert the transform function.

        :param X: The data to perform an inverse transform on.
        :param return: The unscaled version of the data.
        """
        # Round to ensure that very small differences don't
        # cause arrays to become inequal.
        return ((X * self.std) + self.mean)


def shuffle(array):
    """Gpu/cpu-agnostic shuffle function."""
    xp = cp.get_array_module(array)
    z = array.copy()
    if xp == cp:
        z = z.get()
        np.random.shuffle(z)
        return cp.array(z, dtype=array.dtype)
    else:
        np.random.shuffle(z)
        return z


class Som(object):
    """This is a batched version of the basic SOM."""

    def __init__(self,
                 map_dim,
                 data_dim,
                 learning_rate,
                 lrfunc=expo,
                 nbfunc=expo,
                 sigma=None,
                 min_max=np_min):
        """
        A batched Self-Organizing-Map.

        :param map_dim: A tuple describing the MAP size.
        :param data_dim: The dimensionality of the input matrix.
        :param learning_rate: The learning rate.
        :param sigma: The neighborhood factor.
        :param lrfunc: The function used to decrease the learning rate.
        :param nbfunc: The function used to decrease the neighborhood
        :param min_max: The function used to determine the winner.
        """
        if sigma is not None:
            self.sigma = sigma
        else:
            # Add small constant to sigma to prevent
            # divide by zero for maps of size 2.
            self.sigma = (max(map_dim) / 2.0) + 0.01

        self.learning_rate = learning_rate
        # A tuple of dimensions
        # Usually (width, height), but can accomodate one-dimensional maps.
        self.map_dimensions = map_dim

        # The dimensionality of the weight vector
        # Usually (width * height)
        self.weight_dim = int(reduce(np.multiply, map_dim, 1))

        # Weights are initialized to small random values.
        # Initializing to more appropriate values given the dataset
        # will probably give faster convergence.
        self.weights = np.zeros((self.weight_dim, data_dim))
        self.data_dim = data_dim

        # The function used to diminish the learning rate.
        self.lrfunc = lrfunc
        # The function used to diminish the neighborhood size.
        self.nbfunc = nbfunc

        # Initialize the distance grid: only needs to be done once.
        self.distance_grid = None
        self.min_max = min_max
        self.trained = False
        self.scaler = Scaler()

    def fit(self,
            X,
            num_epochs=10,
            init_pca=True,
            total_updates=50,
            stop_lr_updates=1.0,
            stop_nb_updates=1.0,
            batch_size=100,
            show_progressbar=False,
            seed=44):
        """
        Fit the SOM to some data.

        The updates correspond to the number of updates to the parameters
        (i.e. learning rate, neighborhood, not weights!)
        to perform during training.

        In general, 5 to 10 updates will do for most learning problems.
        Doing updates to the neighborhood gets more expensive as the
        size of the map increases. Hence, it is advisable to not make
        the number of updates too big for your learning problem.

        :param X: the data on which to train.
        :param num_epochs: the number of epochs for which to train.
        :param init_pca: Whether to initialize the weights to the
        first principal components of the input data.
        :param total_updates: The number of updates to the parameters to do
        during training.
        :param stop_lr_updates: A fraction, describing over which portion of
        the training data the learning rate should decrease. If the total
        number of updates, for example is 1000, and stop_updates = 0.5,
        1000 updates will have occurred after half of the examples.
        After this period, no updates of the parameters will occur.
        :param stop_nb_updates: A fraction, describing over which portion of
        the training data the neighborhood should decrease.
        :param batch_size: the batch size
        :param show_progressbar: whether to show the progress bar.
        :param seed: The random seed
        :return: None
        """
        xp = cp.get_array_module(X)

        X = xp.asarray(X, dtype=xp.float32)
        self._check_input(X)

        X = self.scaler.fit_transform(X)

        xp.random.seed(seed)

        if init_pca:

            if xp == cp:
                raise ValueError("PCA is currently not supported on GPU.")

            x = xp.arange(len(self.weights)) // self.map_dimensions[1]
            y = xp.arange(len(self.weights)) % self.map_dimensions[1]
            coord = xp.stack([x, y], 1)

            coord = coord / self.map_dimensions[1]
            coord = (coord - .5) * 2

            # Subtract mean from data.
            mean = X.mean(0)
            temp = X - mean
            pca = PCA(n_components=2, svd_solver='randomized')
            pca.fit(temp)
            eigvec = pca.components_
            eigval = pca.explained_variance_
            norms = np.sqrt(np.sum(np.square(eigvec), 1))
            eigvec = ((eigvec.T/norms)*eigval).T

            p = coord[:, :, None] * eigvec[None, :, :]
            self.weights = p.sum(1) + mean
            self.weights = xp.array(self.weights)
        else:
            min_val = X.min(0)
            max_val = X.max(0)
            data_range = min_val + (max_val - min_val)
            self.weights = data_range * xp.random.rand(len(self.weights),
                                                       X.shape[1])

        # The train length
        if batch_size > len(X):
            batch_size = len(X)
        train_len = (len(X) * num_epochs) // batch_size

        batched = self._create_batches(X, batch_size)
        self._ensure_params(batched)
        self.distance_grid = self._initialize_distance_grid(batched)

        # The step size is the number of items between epochs.
        step_size_lr = max((train_len * stop_lr_updates) // total_updates, 1)
        step_size_nb = max((train_len * stop_nb_updates) // total_updates, 1)

        # Precalculate the number of LR updates.
        stop_lr = (train_len * stop_lr_updates)
        lr_update_steps = set(xp.arange(step_size_lr,
                                        stop_lr + step_size_lr,
                                        step_size_lr).tolist())

        # Precalculate the number of LR updates.
        stop_nb = (train_len * stop_nb_updates)
        nb_update_steps = set(xp.arange(step_size_nb,
                                        stop_nb + step_size_nb,
                                        step_size_nb).tolist())

        start = time.time()

        # Train
        nb_step = 0
        lr_step = 0
        idx = 0

        map_radius = self.nbfunc(self.sigma,
                                 0,
                                 len(nb_update_steps))

        learning_rate = self.lrfunc(self.learning_rate,
                                    0,
                                    len(lr_update_steps))

        influences = self._calculate_influence(map_radius) * learning_rate

        for epoch in range(num_epochs):

            logger.info("Epoch {0} of {1}".format(epoch, num_epochs))

            idx, nb_step, lr_step, influences = self._epoch(X,
                                                            nb_update_steps,
                                                            lr_update_steps,
                                                            batch_size,
                                                            idx,
                                                            nb_step,
                                                            lr_step,
                                                            show_progressbar,
                                                            influences)

        self.trained = True
        self.weights = self.scaler.inverse_transform(self.weights)

        logger.info("Total train time: {0}".format(time.time() - start))

    def _ensure_params(self, X):
        """
        Ensure the parameters are of the correct type.

        :param X: The input data
        :return: None
        """
        xp = cp.get_array_module(X)
        self.weights = xp.asarray(self.weights, xp.float32)

    def _init_prev(self, x):
        """
        Placeholder.

        :param x:
        :return:
        """
        return None

    def _epoch(self,
               X,
               nb_update_steps,
               lr_update_steps,
               batch_size,
               idx,
               nb_step,
               lr_step,
               show_progressbar,
               influences):
        """
        Run a single epoch.

        This function uses an index parameter which is passed around to see to
        how many training items the SOM has been exposed globally.

        nb and lr_update_counter hold the indices at which the neighborhood
        size and learning rates need to be updated.
        These are therefore also passed around. The nb_step and lr_step
        parameters indicate how many times the neighborhood
        and learning rate parameters have been updated already.

        :param X: The training data.
        :param nb_update_steps: The indices at which to
        update the neighborhood.
        :param lr_update_steps: The indices at which to
        update the learning rate.
        :param idx: The current index.
        :param nb_step: The current neighborhood step.
        :param lr_step: The current learning rate step.
        :param show_progressbar: Whether to show a progress bar or not.
        :return: The index, neighborhood step and learning rate step
        """
        # Store original data length
        data_len = X.shape[0]
        num_processed = 0

        # Shuffle and batch training data
        X = self._create_batches(X, batch_size)

        # Initialize the previous activation
        prev_activation = self._init_prev(X)

        # Calculate the influences for update 0.
        learning_rate = self.lrfunc(self.learning_rate,
                                    lr_step,
                                    len(lr_update_steps))

        # Iterate over the training data
        for x in tqdm(X, disable=not show_progressbar):

            if num_processed + batch_size > data_len:
                x = x[:data_len - (num_processed + batch_size)]

            prev_activation = self._propagate(x,
                                              influences,
                                              prev_activation=prev_activation)

            if idx in nb_update_steps:

                nb_step += 1

                map_radius = self.nbfunc(self.sigma,
                                         nb_step,
                                         len(nb_update_steps))
                logger.info("Updated map radius: {0}".format(map_radius))

                # The map radius has been updated, so the influence
                # needs to be recalculated
                influences = self._calculate_influence(map_radius)
                influences *= learning_rate

            if idx in lr_update_steps:
                lr_step += 1

                # Reset the influences back to 1
                influences /= learning_rate
                learning_rate = self.lrfunc(self.learning_rate,
                                            lr_step,
                                            len(lr_update_steps))
                logger.info("Updated learning rate: {0}".format(learning_rate))
                # Recalculate the influences
                influences *= learning_rate

            num_processed += batch_size

            idx += 1

        return idx, nb_step, lr_step, influences

    def _create_batches(self, X, batch_size, shuffle_data=True):
        """
        Create batches out of a sequence of data.

        Assumes ndim(X) == 2.

        This function will append zeros to the end of your data to ensure that
        all batches are even-sized.

        :param X: A numpy array, representing your input data.
        Must have 2 dimensions.
        :param batch_size: The desired batch size.
        :return: A batched version of your data.
        """
        xp = cp.get_array_module(X)

        if shuffle_data:
            X = shuffle(X)

        if batch_size > X.shape[0]:
            batch_size = X.shape[0]

        max_x = int(xp.ceil(X.shape[0] / batch_size))
        X = resize(X, (max_x, batch_size, X.shape[1]))

        return X

    def _propagate(self, x, influences, **kwargs):
        """
        Propagate a single batch of examples through the network.

        First computes the activation the maps neurons, given a batch.
        Then updates the weights of the neurons by taking the mean of
        differences over the batch.

        :param X: an array of data
        :param influences: The influence at the current epoch,
        given the learning rate and map size
        :return: A vector describing activation values for each unit.
        """
        activation, difference_x = self.forward(x)
        self.backward(difference_x, influences, activation)

        return activation

    def forward(self, x, **kwargs):
        """
        Get the best matching units, based on euclidean distance.

        :param x: The input vector
        :return: The activations, and
         the differences between the input and the weights, which can be
         reused in the update calculation.
        """
        return self.distance_function(x, self.weights)

    def _calculate_update(self, x, influence):
        """
        Multiply the difference vector with the influence vector.

        The influence vector is chosen based on the BMU, so that the update
        done to the every weight node is proportional to its distance from the
        BMU.

        Implicitly uses Oja's Rule: delta_W = alpha * (X - w)

        In this case (X - w) has been precomputed for speed, in the
        forward step.

        :param x: The difference vector between the weights and the input.
        :param influence: The influence the result has on each unit,
        depending on distance. Already includes the learning rate.
        """
        xp = cp.get_array_module(x)
        return xp.multiply(x, influence)

    def backward(self, diff_x, influences, activation, **kwargs):
        """
        Backward pass through the network, including update.

        :param diff_x: The difference between the input data and the weights
        :param influences: The influences at the current time-step
        :param activation: The activation at the output layer
        :param kwargs:
        :return: None
        """
        influence = self._apply_influences(activation, influences)
        update = self._calculate_update(diff_x, influence)
        self.weights += update.mean(0)

    def distance_function(self, x, weights):
        """
        Calculate euclidean distance between a batch of input data and weights.

        :param x: The input
        :param weights: The weights
        :return: A tuple of matrices,
        The first matrix is a (batch_size * neurons) matrix of
        activation values, containing the response of each neuron
        to each input
        The second matrix is a (batch_size * neuron) matrix containing
        the difference between euch neuron and each input.
        """
        xp = cp.get_array_module(x)
        diff = self._distance_difference(x, weights)
        activations = xp.linalg.norm(diff, axis=2)

        return activations, diff

    def _distance_difference(self, x, weights):
        """
        Calculate the difference between an input and all the weights.

        :param x: The input.
        :param weights: An array of weights.
        :return: A vector of differences.
        """
        return x[:, None, :] - weights[None, :, :]

    def _apply_influences(self, activations, influences):
        """
        Calculate the BMU using min_max, and get the appropriate influences.

        Then gets the appropriate influence from the neighborhood,
        given the BMU

        :param activations: A Numpy array of distances.
        :param influences: A (map_dim, map_dim, data_dim) array
        describing the influence each node has on each other node.
        :return: The influence given the bmu, and the index of the bmu itself.
        """
        bmu = self.min_max(activations, 1)[1]
        xp = cp.get_array_module(activations)
        influences = xp.asarray(influences, dtype=xp.float32)
        return influences[bmu]

    def _calculate_influence(self, sigma):
        """
        Pre-calculate the influence for a given value of sigma.

        The neighborhood has size map_dim * map_dim, so for a 30 * 30 map,
        the neighborhood will be size (900, 900).  d

        :param sigma: The neighborhood value.
        :return: The neighborhood
        """
        xp = cp.get_array_module(self.distance_grid)
        grid = xp.exp(-self.distance_grid / (2.0 * sigma ** 2))
        return grid.reshape(self.weight_dim, self.weight_dim)[:, :, None]

    def _initialize_distance_grid(self, X):
        """
        Initialize the distance grid by calls to _grid_dist.

        :return:
        """
        xp = cp.get_array_module(X)
        p = [self._grid_distance(i) for i in range(self.weight_dim)]
        return xp.array(p, dtype=xp.float32)

    def _grid_distance(self, index):
        """
        Calculate the distance grid for a single index position.

        This is pre-calculated for fast neighborhood calculations
        later on (see _calc_influence).

        :param index: The index for which to calculate the distances.
        :return: A flattened version of the distance array.
        """
        width, height = self.map_dimensions

        row = index // height
        column = index % height

        # Fast way to construct distance matrix
        x = np.abs(np.arange(width) - row) ** 2
        y = np.abs(np.arange(height) - column) ** 2

        distance = x[:, None] + y[None, :]

        return distance

    def _check_input(self, X):
        """
        Check the input for validity.

        Ensures that the input data, X, is a 2-dimensional matrix, and that
        the second dimension of this matrix has the same dimensionality as
        the weight matrix.

        :param X: the input data
        :return: None
        """
        if X.ndim != 2:
            raise ValueError("Your data is not a 2D matrix. "
                             "Actual size: {0}".format(X.shape))

        if X.shape[1] != self.data_dim:
            raise ValueError("Your data size != weight dim: {0}, "
                             "expected {1}".format(X.shape[1], self.data_dim))

    def _predict_base(self, X, batch_size=100, show_progressbar=False):
        """
        Predict distances to some input data.

        This function should not be directly used.

        :param X: The input data.
        :return: A matrix, representing the activation
        each node has to each input.
        """
        self._check_input(X)

        xp = cp.get_array_module()
        batched = self._create_batches(X, batch_size, shuffle_data=False)

        activations = []

        for x in tqdm(batched, disable=not show_progressbar):
            activations.extend(self.forward(x)[0])

        activations = xp.asarray(activations, dtype=xp.float32)
        activations = activations[:X.shape[0]]
        return activations.reshape(X.shape[0], self.weight_dim)

    def predict(self, X, batch_size=100, show_progressbar=True):
        """
        Predict the BMU for each input data.

        :param X: Input data.
        :param batch_size: The batch size to use in prediction.
        :return: The index of the bmu which best describes the input data.
        """
        dist = self._predict_base(X, batch_size, show_progressbar)
        res = self.min_max(dist, 1)[1]
        xp = cp.get_array_module(res)
        if xp == np:
            return res
        else:
            return res.get()

    def quant_error(self, X, batch_size=1):
        """
        Calculate the quantization error.

        Find the the minimum euclidean distance between the units and
        some input.

        :param X: Input data.
        :param batch_size: The batch size to use when calculating
        the quantization error. Recommended to not raise this.
        :return: A vector of numbers, representing the quantization error
        for each data point.
        """
        dist = self._predict_base(X, batch_size)
        res = self.min_max(dist, 1)[0]
        xp = cp.get_array_module(res)
        if xp == np:
            return res
        else:
            return res.get()

    def receptive_field(self,
                        X,
                        identities,
                        max_len=10,
                        threshold=0.9,
                        batch_size=1):
        """
        Calculate the receptive field of the SOM on some data.

        The receptive field is the common ending of all sequences which
        lead to the activation of a given BMU. If a SOM is well-tuned to
        specific sequences, it will have longer receptive fields, and therefore
        gives a better description of the dynamics of a given system.

        :param X: Input data.
        :param identities: The letters associated with each input datum.
        :param max_len: The maximum length sequence we expect.
        Increasing the window size leads to accurate results,
        but costs more memory.
        :param threshold: The threshold at which we consider a receptive field
        valid. If at least this proportion of the sequences of a neuron have
        the same suffix, that suffix is counted as acquired by the SOM.
        :param batch_size: The batch size to use in prediction
        :return: The receptive field of each neuron.
        """
        if len(X) != len(identities):
            raise ValueError("X and identities are not the same length: {0} and {1}".format(len(X), len(identities)))

        receptive_fields = defaultdict(list)
        predictions = self.predict(X, batch_size)

        for idx, p in enumerate(predictions.tolist()):
            receptive_fields[p].append(identities[idx+1 - max_len:idx+1])

        sequence = defaultdict(list)

        for k, v in receptive_fields.items():

            v = [x for x in v if x]

            total = len(v)
            for row in reversed(list(zip(*v))):

                r = Counter(row)

                for _, count in r.items():
                    if count / total > threshold:
                        sequence[k].append(row[0])
                    else:
                        break

        return {k: v[::-1] for k, v in sequence.items()}

    def invert_projection(self, X, identities):
        """
        Calculate the inverted projection.

        The inverted projectio of a SOM is created by association each weight
        with the input datum which matches it the most.
        Works best for symbolic (instead of continuous) input data.

        :param X: Input data
        :param identities: The identities for each
        input datum, must be same length as X
        :return: A numpy array with identities, the shape of the map.
        """
        xp = cp.get_array_module(X)

        if len(X) != len(identities):
            raise ValueError("X and identities are not the same length: {0} and {1}".format(len(X), len(identities)))

        # Find all unique items in X
        X_unique, indices = np.unique(X, return_index=True, axis=0)

        node_match = []

        distances, _ = self.distance_function(X_unique, self.weights)

        if xp != np:
            distances = distances.get()

        print(distances.shape)

        for d in distances.argmin(0):
            node_match.append(identities[indices[d]])

        return node_match

    def map_weights(self):
        """
        Retrieve the grid as a list of lists of weights.

        :return: A three-dimensional Numpy array of values.
        """
        width, height = self.map_dimensions
        # Reshape to appropriate dimensions
        w = self.scaler.inverse_transform(self.weights)
        return w.reshape((width, height, self.data_dim)).transpose(1, 0, 2)

    @classmethod
    def load(cls, path, array_type=np):
        """
        Load a SOM from a JSON file.

        :param path: The path to the JSON file.
        :param array_type: The array type to use.
        Defaults to np, can be eiter numpy or cupy
        :return: A SOM.
        """
        data = json.load(open(path))

        weights = data['weights']
        weights = array_type.asarray(weights, dtype=array_type.float32)
        datadim = weights.shape[1]

        dimensions = data['dimensions']
        lrfunc = expo if data['lrfunc'] == 'expo' else linear
        nbfunc = expo if data['nbfunc'] == 'expo' else linear
        lr = data['lr']
        sigma = data['sigma']

        s = cls(dimensions,
                datadim,
                lr,
                lrfunc=lrfunc,
                nbfunc=nbfunc,
                sigma=sigma)
        s.weights = weights
        s.trained = True

        return s

    def save(self, path):
        """
        Save a SOM to a JSON file.

        :param path: The path to the JSON file that will be created
        :return: None
        """
        to_save = {}
        to_save['weights'] = [[float(w) for w in x] for x in self.weights]
        to_save['dimensions'] = self.map_dimensions
        to_save['lrfunc'] = 'expo' if self.lrfunc == expo else 'linear'
        to_save['nbfunc'] = 'expo' if self.nbfunc == expo else 'linear'
        to_save['lr'] = self.learning_rate
        to_save['sigma'] = self.sigma

        json.dump(to_save, open(path, 'w'))
