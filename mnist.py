#  Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Convolutional Neural Network Estimator for MNIST, built with tf.layers."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import base64
import json
import os

from absl import app as absl_app
from absl import flags
import tensorflow as tf  # pylint: disable=g-bad-import-order

import dataset
from utils.flags import core as flags_core
from utils.logs import hooks_helper
from utils.misc import distribution_utils
from utils.misc import model_helpers

LEARNING_RATE = 1e-4


def create_model(data_format):
    """Model to recognize digits in the MNIST dataset.
    uses the tf.keras API.
    Args:
      data_format: Either 'channels_first' or 'channels_last'. 'channels_first' is
        typically faster on GPUs while 'channels_last' is typically faster on
        CPUs. See
        https://www.tensorflow.org/performance/performance_guide#data_formats
    Returns:
      A tf.keras.Model.
    """
    if data_format == 'channels_first':
        input_shape = [1, 28, 28]
    else:
        assert data_format == 'channels_last'
        input_shape = [28, 28, 1]

    l = tf.keras.layers
    max_pool = l.MaxPooling2D(
        (2, 2), (2, 2), padding='same', data_format=data_format)
    # The model consists of a sequential chain of layers, so tf.keras.Sequential
    # (a subclass of tf.keras.Model) makes for a compact description.
    return tf.keras.Sequential(
        [
            l.Reshape(
                target_shape=input_shape,
                input_shape=(28 * 28,)),
            l.Conv2D(
                32,
                5,
                padding='same',
                data_format=data_format,
                activation=tf.nn.relu),
            max_pool,
            l.Conv2D(
                64,
                5,
                padding='same',
                data_format=data_format,
                activation=tf.nn.relu),
            max_pool,
            l.Flatten(),
            l.Dense(1024, activation=tf.nn.relu),
            l.Dropout(0.4),
            l.Dense(10)
        ])


def get_tf_config():
    tf_config = os.environ.get('TF_CONFIG')
    if not tf_config:
        return
    return json.loads(tf_config)


def get_paperspace_tf_config():
    tf_config = os.environ.get('TF_CONFIG')
    if not tf_config:
        return
    paperspace_tf_config = json.loads(base64.urlsafe_b64decode(tf_config).decode('utf-8'))

    tf.logging.debug(str(paperspace_tf_config))
    return paperspace_tf_config


def set_tf_config():
    tf_config = get_paperspace_tf_config()
    if tf_config:
        os.environ['TF_CONFIG'] = json.dumps(tf_config)


def define_mnist_flags():
    flags_core.define_base()
    flags_core.define_performance(num_parallel_calls=False)
    flags_core.define_image()
    data_dir = os.path.abspath(os.environ.get('PS_JOBSPACE', os.getcwd()) + '/data')
    model_dir = os.path.abspath(os.environ.get('PS_MODEL_PATH', os.getcwd() + '/models') + '/mnist')
    flags.adopt_module_key_flags(flags_core)
    flags_core.set_defaults(data_dir=data_dir,
                            model_dir=model_dir,
                            export_dir=os.environ.get('PS_MODEL_PATH', os.getcwd() + '/models'),
                            batch_size=int(os.environ.get('batch_size', 100)),
                            train_epochs=int(os.environ.get('train_epochs', 20)))


def model_fn(features, labels, mode, params):
    """The model_fn argument for creating an Estimator."""
    model = create_model(params['data_format'])
    image = features
    if isinstance(image, dict):
        image = features['image']

    if mode == tf.estimator.ModeKeys.PREDICT:
        logits = model(image, training=False)
        predictions = {
            'classes': tf.argmax(logits, axis=1),
            'probabilities': tf.nn.softmax(logits),
        }
        return tf.estimator.EstimatorSpec(
            mode=tf.estimator.ModeKeys.PREDICT,
            predictions=predictions,
            export_outputs={
                'classify': tf.estimator.export.PredictOutput(predictions)
            })
    if mode == tf.estimator.ModeKeys.TRAIN:
        optimizer = tf.train.AdamOptimizer(learning_rate=LEARNING_RATE)

        logits = model(image, training=True)
        loss = tf.losses.sparse_softmax_cross_entropy(labels=labels, logits=logits)
        accuracy = tf.metrics.accuracy(
            labels=labels, predictions=tf.argmax(logits, axis=1))

        # Name tensors to be logged with LoggingTensorHook.
        tf.identity(LEARNING_RATE, 'learning_rate')
        tf.identity(loss, 'cross_entropy')
        tf.identity(accuracy[1], name='train_accuracy')

        # Save accuracy scalar to Tensorboard output.
        tf.summary.scalar('train_accuracy', accuracy[1])

        return tf.estimator.EstimatorSpec(
            mode=tf.estimator.ModeKeys.TRAIN,
            loss=loss,
            train_op=optimizer.minimize(loss, tf.train.get_or_create_global_step()))
    if mode == tf.estimator.ModeKeys.EVAL:
        logits = model(image, training=False)
        loss = tf.losses.sparse_softmax_cross_entropy(labels=labels, logits=logits)
        return tf.estimator.EstimatorSpec(
            mode=tf.estimator.ModeKeys.EVAL,
            loss=loss,
            eval_metric_ops={
                'accuracy':
                    tf.metrics.accuracy(
                        labels=labels, predictions=tf.argmax(logits, axis=1)),
            })


def run_mnist(flags_obj):
    """Run MNIST training and eval loop.
    Args:
      flags_obj: An object containing parsed flag values.
    """
    model_helpers.apply_clean(flags_obj)
    model_function = model_fn

    session_config = tf.ConfigProto(
        inter_op_parallelism_threads=flags_obj.inter_op_parallelism_threads,
        intra_op_parallelism_threads=flags_obj.intra_op_parallelism_threads,
        allow_soft_placement=True)

    distribution_strategy = distribution_utils.get_distribution_strategy(
        flags_core.get_num_gpus(flags_obj), flags_obj.all_reduce_alg)

    run_config = tf.estimator.RunConfig(
        train_distribute=distribution_strategy, session_config=session_config)

    data_format = flags_obj.data_format
    if data_format is None:
        data_format = ('channels_first'
                       if tf.test.is_built_with_cuda() else 'channels_last')
    mnist_classifier = tf.estimator.Estimator(
        model_fn=model_function,
        model_dir=flags_obj.model_dir,
        config=run_config,
        params={
            'data_format': data_format,
        })

    # Set up training and evaluation input functions.
    def train_input_fn():
        """Prepare data for training."""

        # When choosing shuffle buffer sizes, larger sizes result in better
        # randomness, while smaller sizes use less memory. MNIST is a small
        # enough dataset that we can easily shuffle the full epoch.
        ds = dataset.train(flags_obj.data_dir)
        ds = ds.cache().shuffle(buffer_size=50000).batch(flags_obj.batch_size)

        # Iterate through the dataset a set number (`epochs_between_evals`) of times
        # during each training session.
        ds = ds.repeat(flags_obj.epochs_between_evals)
        return ds

    def eval_input_fn():
        return dataset.test(flags_obj.data_dir).batch(
            flags_obj.batch_size).make_one_shot_iterator().get_next()

    # Set up hook that outputs training logs every 100 steps.
    train_hooks = hooks_helper.get_train_hooks(
        flags_obj.hooks, model_dir=flags_obj.model_dir,
        batch_size=flags_obj.batch_size)

    train_spec = tf.estimator.TrainSpec(input_fn=train_input_fn, hooks=train_hooks, max_steps=10000)
    eval_spec = tf.estimator.EvalSpec(input_fn=eval_input_fn, steps=None,
                                      start_delay_secs=0,
                                      throttle_secs=60)

    tf.estimator.train_and_evaluate(mnist_classifier, train_spec, eval_spec)

    # Export the model
    if flags_obj.export_dir is not None:
        image = tf.placeholder(tf.float32, [None, 28, 28])
        input_fn = tf.estimator.export.build_raw_serving_input_receiver_fn({
            'image': image,
        })
        mnist_classifier.export_savedmodel(flags_obj.export_dir, input_fn,
                                           strip_default_attrs=True)


def main(_):
    run_mnist(flags.FLAGS)


if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.INFO)
    set_tf_config()
    define_mnist_flags()
    absl_app.run(main)
