# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import argparse
import os
import numpy as np

import paddle
from paddle import inference

from paddlenlp.data import Tuple, Pad
from paddlenlp.transformers import AutoTokenizer
from paddlenlp.datasets import load_dataset
from paddlenlp.utils.log import logger


def convert_example(example, tokenizer, max_seq_length=512, is_test=False):

    query, title = example["query"], example["title"]

    encoded_inputs = tokenizer(text=query,
                               text_pair=title,
                               max_seq_len=max_seq_length)

    input_ids = encoded_inputs["input_ids"]
    token_type_ids = encoded_inputs["token_type_ids"]

    if not is_test:
        label = np.array([example["label"]], dtype="int64")
        return input_ids, token_type_ids, label
    else:
        return input_ids, token_type_ids


class Predictor(object):

    def __init__(self,
                 model_dir,
                 device="gpu",
                 max_seq_length=128,
                 batch_size=32,
                 use_tensorrt=False,
                 precision="fp32",
                 cpu_threads=10,
                 enable_mkldnn=False,
                 benchmark=False,
                 save_log_path="./log_output"):
        self.max_seq_length = max_seq_length
        self.batch_size = batch_size
        self.benchmark = benchmark

        model_file = os.path.join(model_dir, "inference.pdmodel")
        params_file = os.path.join(model_dir, "inference.pdiparams")
        if not os.path.exists(model_file):
            raise ValueError("not find model file path {}".format(model_file))
        if not os.path.exists(params_file):
            raise ValueError("not find params file path {}".format(params_file))
        config = paddle.inference.Config(model_file, params_file)

        if device == "gpu":
            # set GPU configs accordingly
            # such as intialize the gpu memory, enable tensorrt
            config.enable_use_gpu(100, 0)
            precision_map = {
                "fp16": inference.PrecisionType.Half,
                "fp32": inference.PrecisionType.Float32,
                "int8": inference.PrecisionType.Int8
            }
            precision_mode = precision_map[precision]

            if use_tensorrt:
                config.enable_tensorrt_engine(max_batch_size=batch_size,
                                              min_subgraph_size=30,
                                              precision_mode=precision_mode)
        elif device == "cpu":
            # set CPU configs accordingly,
            # such as enable_mkldnn, set_cpu_math_library_num_threads
            config.disable_gpu()
            if enable_mkldnn:
                # cache 10 different shapes for mkldnn to avoid memory leak
                config.set_mkldnn_cache_capacity(10)
                config.enable_mkldnn()
            config.set_cpu_math_library_num_threads(cpu_threads)
        elif device == "xpu":
            # set XPU configs accordingly
            config.enable_xpu(100)

        config.switch_use_feed_fetch_ops(False)
        self.predictor = paddle.inference.create_predictor(config)
        self.input_handles = [
            self.predictor.get_input_handle(name)
            for name in self.predictor.get_input_names()
        ]
        self.output_handle = self.predictor.get_output_handle(
            self.predictor.get_output_names()[0])

        if benchmark:
            import auto_log
            pid = os.getpid()
            self.autolog = auto_log.AutoLogger(model_name="ernie-tiny",
                                               model_precision=precision,
                                               batch_size=self.batch_size,
                                               data_shape="dynamic",
                                               save_path=save_log_path,
                                               inference_config=config,
                                               pids=pid,
                                               process_name=None,
                                               gpu_ids=0,
                                               time_keys=[
                                                   'preprocess_time',
                                                   'inference_time',
                                                   'postprocess_time'
                                               ],
                                               warmup=0,
                                               logger=logger)

    def predict(self, data, tokenizer, label_map):
        """
        Predicts the data labels.

        Args:
            data (obj:`List(str)`): The batch data whose each element is a raw text.
            tokenizer(obj:`PretrainedTokenizer`): This tokenizer inherits from :class:`~paddlenlp.transformers.PretrainedTokenizer` 
                which contains most of the methods. Users should refer to the superclass for more information regarding methods.
            label_map(obj:`dict`): The label id (key) to label str (value) map.

        Returns:
            results(obj:`dict`): All the predictions labels.
        """
        if self.benchmark:
            self.autolog.times.start()

        examples = []
        for text in data:
            input_ids, segment_ids = convert_example(
                text,
                tokenizer,
                max_seq_length=self.max_seq_length,
                is_test=True)
            examples.append((input_ids, segment_ids))

        batchify_fn = lambda samples, fn=Tuple(
            Pad(axis=0, pad_val=tokenizer.pad_token_id),  # input
            Pad(axis=0, pad_val=tokenizer.pad_token_id),  # segment
        ): fn(samples)

        if self.benchmark:
            self.autolog.times.stamp()

        input_ids, segment_ids = batchify_fn(examples)
        self.input_handles[0].copy_from_cpu(input_ids)
        self.input_handles[1].copy_from_cpu(segment_ids)
        self.predictor.run()
        probs = self.output_handle.copy_to_cpu()
        if self.benchmark:
            self.autolog.times.stamp()

        #probs = softmax(logits, axis=1)
        idx = np.argmax(probs, axis=1)
        idx = idx.tolist()
        labels = [label_map[i] for i in idx]

        if args.benchmark:
            self.autolog.times.end(stamp=True)

        return labels


if __name__ == "__main__":
    # yapf: disable
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir", type=str, required=True, help="The directory to static model.")
    parser.add_argument("--max_seq_length", default=128, type=int, help="The maximum total input sequence length after tokenization. Sequences longer than this will be truncated, sequences shorter will be padded.")
    parser.add_argument("--batch_size", default=32, type=int, help="Batch size per GPU/CPU for training.")
    parser.add_argument('--device', choices=['cpu', 'gpu', 'xpu'], default="gpu", help="Select which device to train model, defaults to gpu.")
    parser.add_argument('--use_tensorrt', default=False, type=eval, choices=[True, False], help='Enable to use tensorrt to speed up.')
    parser.add_argument("--precision", default="fp32", type=str, choices=["fp32", "fp16", "int8"], help='The tensorrt precision.')
    parser.add_argument('--cpu_threads', default=10, type=int, help='Number of threads to predict when using cpu.')
    parser.add_argument('--enable_mkldnn', default=False, type=eval, choices=[True, False], help='Enable to use mkldnn to speed up when using cpu.')
    parser.add_argument("--benchmark", type=eval, default=False, help="To log some information about environment and running.")
    parser.add_argument("--save_log_path", type=str, default="./log_output/", help="The file path to save log.")
    parser.add_argument("--max_steps", default=-1, type=int, help="If > 0: set total number of predict steps to perform.")

    args = parser.parse_args()
    # yapf: enable

    # Define predictor to do prediction.
    predictor = Predictor(args.model_dir, args.device, args.max_seq_length,
                          args.batch_size, args.use_tensorrt, args.precision,
                          args.cpu_threads, args.enable_mkldnn, args.benchmark,
                          args.save_log_path)

    tokenizer = AutoTokenizer.from_pretrained('ernie-3.0-medium-zh')

    test_ds = load_dataset("lcqmc", splits=["test"])

    data = [{'query': d['query'], 'title': d['title']} for d in test_ds]
    if args.max_steps > 0:
        data = data[:args.max_steps]

    batches = [
        data[idx:idx + args.batch_size]
        for idx in range(0, len(data), args.batch_size)
    ]
    label_map = {0: 'dissimilar', 1: 'similar'}

    results = []
    for batch_data in batches:
        results.extend(predictor.predict(batch_data, tokenizer, label_map))
    for idx, text in enumerate(data):
        print('Data: {} \t Label: {}'.format(text, results[idx]))
    if args.benchmark:
        predictor.autolog.report()
