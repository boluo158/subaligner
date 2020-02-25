import datetime
import os
import h5py
import traceback
import concurrent.futures
import numpy as np
import tensorflow as tf
import multiprocessing as mp

from .network import Network
from .media_helper import MediaHelper
from .exception import UnsupportedFormatException, TerminalException
from .logger import Logger
from .utils import Utils
Utils.suppress_lib_logs()


class Trainer(object):
    """Network trainer.
    """

    __LOGGER = Logger().get_logger(__name__)
    __MAX_BYTES = 2 ** 31 - 1

    def __init__(self, feature_embedder):
        """Initialiser for the training process.

        Arguments:
            feature_embedder {Embedder.FeatureEmbedder} -- the feature embedder object.

        Raises:
            NotImplementedError -- Thrown when any Trainer attributes are modified.
        """

        self.__feature_embedder = feature_embedder

        # freeze the object after creation
        def __setattr__(self, *args):
            raise NotImplementedError("Cannot modify the immutable object")

        def __delattr__(self, *args):
            raise NotImplementedError("Cannot modify the immutable object")

    def train(
        self,
        av_file_paths,
        subtitle_file_paths,
        model_dir,
        weights_dir,
        logs_dir,
        training_dump_dir,
        epochs=1000,
        training_log="training.log",
        resume=False,
    ):
        """Trigger the training process.

        Arguments:
            av_file_paths {list} -- A list of paths to the input audio/video files.
            subtitle_file_paths {list} -- A list of paths to the subtitle files.
            model_dir {string} -- The directory of the model file.
            weights_dir {string} -- The directory of the weights file.
            logs_dir {string} -- The directory of the log file.
            resume {bool} -- True to continue with previous training result or False to start a new one (default: {False}).

        Keyword Arguments:
            training_dump_dir {string} --  The directory of the training data dump file.
            epochs {int} -- The number of training epochs (default: {1000}).
            training_log {string} -- The path to the log file of epoch results (default: {"training.log"}).
        """

        training_start = datetime.datetime.now()
        model_filepath = "{0}/{1}".format(model_dir, "model.hdf5")
        weights_filepath = "{0}/{1}".format(weights_dir, "weights.hdf5")

        if av_file_paths is None or subtitle_file_paths is None:
            # Load the data and labels dump from the disk
            training_dump = training_dump_dir + "/training_dump.hdf5"
            Trainer.__LOGGER.debug(
                "Resume training on data dump: ".format(
                    training_dump
                )
            )
            with h5py.File(training_dump, "r") as hf:
                train_data_raw = hf["train_data"]
                labels_raw = hf["labels"]

                if resume:
                    network = Network.load_model_and_weights(model_filepath, weights_filepath)
                else:
                    input_shape = (train_data_raw.shape[2], train_data_raw.shape[1])
                    Trainer.__LOGGER.debug("input_shape: {}".format(input_shape))
                    network = Network.get_lstm(input_shape)

                val_loss, val_acc = network.fit_with_generator(
                    train_data_raw,
                    labels_raw,
                    model_filepath,
                    weights_filepath,
                    logs_dir,
                    epochs,
                    training_log,
                    resume,
                )
        else:
            train_data, labels = Trainer.__extract_data_and_label_from_avs(
                self, av_file_paths, subtitle_file_paths
            )

            # Dump extracted data and labels to files for re-training
            training_dump = training_dump_dir + "/training_dump.hdf5"
            with h5py.File(training_dump, "w") as hf:
                hf.create_dataset("train_data", data=train_data)
                hf.create_dataset("labels", data=labels)

                rand = np.random.permutation(np.arange(len(labels)))
                train_data = train_data[rand]
                labels = labels[rand]

                train_data = np.array(
                    [np.rot90(m=val, k=1, axes=(0, 1)) for val in train_data]
                )
                train_data = train_data - np.mean(train_data, axis=0)

                input_shape = (train_data.shape[1], train_data.shape[2])
                Trainer.__LOGGER.debug("input_shape: {}".format(input_shape))

                network = Network.get_lstm(input_shape)
                val_loss, val_acc = network.fit_and_get_history(
                    train_data,
                    labels,
                    model_filepath,
                    weights_filepath,
                    logs_dir,
                    epochs,
                    training_log,
                    False,
                )

        Trainer.__LOGGER.debug("val_loss: {}".format(min(val_loss)))
        Trainer.__LOGGER.debug("val_acc: {}".format(max(val_acc)))
        Trainer.__LOGGER.info(
            "Total training time: {}".format(
                str(datetime.datetime.now() - training_start)
            )
        )

        # Save the model together with the weights after training
        combined_filepath = "{0}/combined.hdf5".format(model_dir)
        network.save_model_and_weights(
            model_filepath, weights_filepath, combined_filepath
        )

    def __extract_data_and_label_from_avs(
        self, av_file_paths, subtitle_file_paths
    ):
        """Generate a training dataset and labels from audio/video files.

        Arguments:
            av_file_paths {list} -- A list of paths to the input audio/video files.
            subtitle_file_paths {list} -- A list of paths to the subtitle files.

        Returns:
            tuple -- The training data and labels.
        """

        train_data, labels = (
            [None] * len(av_file_paths),
            [None] * len(subtitle_file_paths),
        )

        extraction_start = datetime.datetime.now()
        max_workers = int(os.getenv("MAX_WORKERS", mp.cpu_count() / 2))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers
        ) as executor:
            futures = [
                executor.submit(
                    Trainer.__extract_in_multithreads,
                    self,
                    index,
                    av_file_paths[index],
                    subtitle_file_paths[index],
                    train_data,
                    labels,
                )
                for index in range(len(av_file_paths))
            ]
            done, not_done = concurrent.futures.wait(futures)
            for future in not_done:
                try:
                    audio_file_path, subtitle_file_path = future.result()
                    Trainer.__LOGGER.error(
                        "Data and label extraction not done: [Audio: {}, Subtitle: {}]".format(
                            audio_file_path, subtitle_file_path
                        )
                    )
                except Exception as e:
                    Trainer.__LOGGER.error(
                        "Unexpected exception: {} stacktrace: {}".format(
                            str(e), traceback.format_stack()
                        )
                    )

        train_data = [x for x in train_data if x is not None]
        labels = [x for x in labels if x is not None]

        train_data = np.concatenate(train_data)
        labels = np.concatenate(labels)
        Trainer.__LOGGER.debug(
            "Data and labels extracted after {} seconds".format(
                str(datetime.datetime.now() - extraction_start)
            )
        )

        return train_data, labels

    def __extract_in_multithreads(
        self, index, av_file_path, subtitle_file_path, train_data, labels
    ):
        file_name, file_ext = os.path.splitext(av_file_path)

        try:
            if file_ext not in MediaHelper.AUDIO_FILE_EXTENSION:
                t = datetime.datetime.now()
                audio_file_path = MediaHelper.extract_audio(
                    av_file_path, True, 16000
                )
                Trainer.__LOGGER.debug(
                    "- Audio extracted after {}".format(
                        str(datetime.datetime.now() - t)
                    )
                )
            else:
                audio_file_path = av_file_path

            x, y = self.__feature_embedder.extract_data_and_label_from_audio(
                audio_file_path,
                subtitle_file_path,
                subtitles=None,
                ignore_sound_effects=True,
            )
        # Some media are malformed and on occurring they will be logged but the expensive training process shall continue
        except (UnsupportedFormatException, TerminalException) as e:
            Trainer.__LOGGER.error(
                "Exception: {}; stacktrace: {}".format(
                    str(e), traceback.format_stack()
                )
            )
            Trainer.__LOGGER.error(
                "[Audio: {}, Subtitle: {}]".format(
                    audio_file_path, subtitle_file_path
                )
            )
        except Exception as e:
            Trainer.__LOGGER.error(
                "Unexpected exception: {}; stacktrace: {}".format(
                    str(e), traceback.format_stack()
                )
            )
            Trainer.__LOGGER.error(
                "[Audio: {}, Subtitle: {}]".format(
                    audio_file_path, subtitle_file_path
                )
            )
        else:
            train_data[index] = x
            labels[index] = y
        return audio_file_path, subtitle_file_path

    def __convert_to_pb_model(self, pb_model_dir, pb_model_name):
        """Covert a HDF 5 model to a protobuf model.

        Arguments:
            pb_model_dir {string} -- The path to the protobuf model file.
            pb_model_name {string} -- The name of the protobuf model.
        """

        saver = tf.compat.v1.train.Saver()
        saver.save(
            self.__backend.get_session(),
            os.path.join(pb_model_dir, "{}.ckpt".format(pb_model_name)),
        )

    def __create_saved_model(self, saved_model_path):
        """Create a saved model.

        Arguments:
            saved_model_path {string} -- The path to the saved model.
        """

        saved_model_builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(
            saved_model_path
        )
        saved_model_builder.add_meta_graph_and_variables(
            self.__backend.get_session(),
            [tf.saved_model.SERVING],
        )
        saved_model_builder.save()