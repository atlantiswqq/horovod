# Copyright 2020 Uber Technologies, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import tensorflow as tf
import horovod.tensorflow as hvd

# Horovod: initialize Horovod.
hvd.init()

# Horovod: pin GPU to be used to process local rank (one GPU per process)
gpus = tf.config.experimental.list_physical_devices('GPU')
for gpu in gpus:
    tf.config.experimental.set_memory_growth(gpu, True)
if gpus:
    tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')

(mnist_images, mnist_labels), _ = \
    tf.keras.datasets.mnist.load_data(path='mnist-%d.npz' % hvd.rank())

dataset = tf.data.Dataset.from_tensor_slices(
    (tf.cast(mnist_images[..., tf.newaxis] / 255.0, tf.float32),
     tf.cast(mnist_labels, tf.int64))
)
dataset = dataset.repeat().shuffle(10000).batch(128)

mnist_model = tf.keras.Sequential([
    tf.keras.layers.Conv2D(32, [3, 3], activation='relu'),
    tf.keras.layers.Conv2D(64, [3, 3], activation='relu'),
    tf.keras.layers.MaxPooling2D(pool_size=(2, 2)),
    tf.keras.layers.Dropout(0.25),
    tf.keras.layers.Flatten(),
    tf.keras.layers.Dense(128, activation='relu'),
    tf.keras.layers.Dropout(0.5),
    tf.keras.layers.Dense(10, activation='softmax')
])
loss = tf.losses.SparseCategoricalCrossentropy()

# Horovod: adjust learning rate based on number of GPUs.
lr = 0.001
opt = tf.optimizers.Adam(lr * hvd.size())

checkpoint_dir = './checkpoints'
checkpoint = tf.train.Checkpoint(model=mnist_model, optimizer=opt)


@tf.function
def training_step(images, labels, first_batch):
    with tf.GradientTape() as tape:
        probs = mnist_model(images, training=True)
        loss_value = loss(labels, probs)

    # Horovod: add Horovod Distributed GradientTape.
    tape = hvd.DistributedGradientTape(tape)

    grads = tape.gradient(loss_value, mnist_model.trainable_variables)
    opt.apply_gradients(zip(grads, mnist_model.trainable_variables))

    # Horovod: broadcast initial variable states from rank 0 to all other processes.
    # This is necessary to ensure consistent initialization of all workers when
    # training is started with random weights or restored from a checkpoint.
    #
    # Note: broadcast should be done after the first gradient step to ensure optimizer
    # initialization.
    if first_batch:
        hvd.broadcast_variables(mnist_model.variables, root_rank=0)
        hvd.broadcast_variables(opt.variables(), root_rank=0)

    return loss_value


@hvd.elastic.run
def train(state):
    # Horovod: adjust number of steps based on number of GPUs.
    last_batch = state.batch
    for batch_idx, (images, labels) in enumerate(dataset.skip(state.batch).take(10000 // hvd.size())):
        state.batch = last_batch + batch_idx
        loss_value = training_step(images, labels, state.batch == 0)

        if state.batch % 10 == 0 and hvd.local_rank() == 0:
            print('Step #%d\tLoss: %.6f' % (state.batch, loss_value))
        state.commit()


def on_state_reset():
    tf.keras.backend.set_value(mnist_model.optimizer.lr, lr * hvd.size())


state = hvd.elastic.TensorFlowKerasState(mnist_model, opt, batch=0)
state.register_reset_callbacks([on_state_reset])

train(state)

# Horovod: save checkpoints only on worker 0 to prevent other workers from
# corrupting it.
if hvd.rank() == 0:
    checkpoint.save(checkpoint_dir)