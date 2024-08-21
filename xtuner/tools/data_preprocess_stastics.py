from xtuner.utils import IGNORE_INDEX
from transformers import AutoConfig, AutoTokenizer
from torchvision.transforms.functional import InterpolationMode
from PIL import Image
from mmengine.fileio import get
from mmengine import print_log
import torchvision.transforms as T
import warnings
import random
import io
import copy
import json
from multiprocessing import Manager
import multiprocessing
import argparse
from tqdm import tqdm
from functools import partial
import os
import numpy as np
from copy import deepcopy
import torch

from transformers import AutoTokenizer
from torch.utils.data import Dataset
PROCESSES = 64


# Referenced from InternVL
def find_closest_aspect_ratio(aspect_ratio, target_ratios, width, height,
                              image_size):
    best_ratio_diff = float('inf')
    best_ratio = (1, 1)
    area = width * height
    for ratio in target_ratios:
        target_aspect_ratio = ratio[0] / ratio[1]
        ratio_diff = abs(aspect_ratio - target_aspect_ratio)
        if ratio_diff < best_ratio_diff:
            best_ratio_diff = ratio_diff
            best_ratio = ratio
        elif ratio_diff == best_ratio_diff:
            if area > 0.5 * image_size * image_size * ratio[0] * ratio[1]:
                best_ratio = ratio
    return best_ratio


def dynamic_preprocess(image,
                       min_num=1,
                       max_num=6,
                       image_size=448,
                       use_thumbnail=False):
    orig_width, orig_height = image.size
    aspect_ratio = orig_width / orig_height

    # calculate the existing image aspect ratio
    target_ratios = {(i, j)
                     for n in range(min_num, max_num + 1)
                     for i in range(1, n + 1) for j in range(1, n + 1)
                     if i * j <= max_num and i * j >= min_num}
    target_ratios = sorted(target_ratios, key=lambda x: x[0] * x[1])

    # find the closest aspect ratio to the target
    target_aspect_ratio = find_closest_aspect_ratio(aspect_ratio,
                                                    target_ratios, orig_width,
                                                    orig_height, image_size)

    # calculate the target width and height
    target_width = image_size * target_aspect_ratio[0]
    target_height = image_size * target_aspect_ratio[1]
    blocks = target_aspect_ratio[0] * target_aspect_ratio[1]

    # resize the image
    resized_img = image.resize((target_width, target_height))
    processed_images = []
    for i in range(blocks):
        box = ((i % (target_width // image_size)) * image_size,
               (i // (target_width // image_size)) * image_size,
               ((i % (target_width // image_size)) + 1) * image_size,
               ((i // (target_width // image_size)) + 1) * image_size)
        # split the image
        split_img = resized_img.crop(box)
        processed_images.append(split_img)
    assert len(processed_images) == blocks
    if use_thumbnail and len(processed_images) != 1:
        thumbnail_img = image.resize((image_size, image_size))
        processed_images.append(thumbnail_img)
    return processed_images


def total_image_token(orig_size,
                      min_num=1,
                      max_num=12,
                      image_size=448,
                      use_thumbnail=True):
    orig_width, orig_height = orig_size

    aspect_ratio = orig_width / orig_height

    # calculate the existing image aspect ratio
    target_ratios = {(i, j)
                     for n in range(min_num, max_num + 1)
                     for i in range(1, n + 1) for j in range(1, n + 1)
                     if max_num >= i * j >= min_num}
    target_ratios = sorted(target_ratios, key=lambda x: x[0] * x[1])

    # find the closest aspect ratio to the target
    target_aspect_ratio = find_closest_aspect_ratio(aspect_ratio,
                                                    target_ratios, orig_width,
                                                    orig_height, image_size)
    blocks = target_aspect_ratio[0] * target_aspect_ratio[1]

    if use_thumbnail and blocks != 1:
        blocks += 1

    return blocks


def load_json_or_jsonl(json_path):
    if json_path.endswith('.json'):
        with open(json_path) as f:
            data = json.load(f)
    elif json_path.endswith('.jsonl'):
        with open(json_path) as f:
            data = [json.loads(line) for line in f]
    else:
        raise ValueError(f'Unsupported file format: {json_path}, '
                         f'only support .json and .jsonl.')
    return data


class InternVL_V1_5_Dataset(Dataset):
    os.environ['TOKENIZERS_PARALLELISM'] = 'true'
    IMG_CONTEXT_TOKEN = '<IMG_CONTEXT>'
    IMG_START_TOKEN = '<img>'
    IMG_END_TOKEN = '</img>'

    IMAGENET_MEAN = (0.485, 0.456, 0.406)
    IMAGENET_STD = (0.229, 0.224, 0.225)

    def __init__(self,
                 model_path,
                 template,
                 data_paths,
                 image_folders=None,
                 repeat_times=1,
                 max_length=8192):
        self.template = template
        self.max_length = max_length

        self.cfg = AutoConfig.from_pretrained(
            model_path, trust_remote_code=True)

        # The following modifications are only to ensure full
        # consistency with the official template,
        # without investigating the impact on performance.
        if self.cfg.llm_config.architectures[0] == 'Phi3ForCausalLM':
            self._system = 'You are an AI assistant whose name is Phi-3.'
            self.template[
                'INSTRUCTION'] = '<|user|>\n{input}<|end|><|assistant|>\n'
        elif self.cfg.llm_config.architectures[0] == 'InternLM2ForCausalLM':
            self._system = 'You are an AI assistant whose name ' \
                           'is InternLM (书生·浦语).'
            self.template['SYSTEM'] = '<|im_start|>system\n{system}<|im_end|>'
            self.template[
                'INSTRUCTION'] = '<|im_start|>user\n{input}' \
                                 '<|im_end|><|im_start|>assistant\n'
        else:
            raise NotImplementedError

        self.min_dynamic_patch = self.cfg.min_dynamic_patch
        self.max_dynamic_patch = self.cfg.max_dynamic_patch
        self.downsample_ratio = self.cfg.downsample_ratio
        self.image_size = self.cfg.force_image_size
        self.use_thumbnail = self.cfg.use_thumbnail
        patch_size = self.cfg.vision_config.patch_size
        self.patch_token = int(
            (self.image_size // patch_size)**2 * (self.downsample_ratio**2))
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, trust_remote_code=True)
        self.transformer = T.Compose([
            T.Lambda(lambda img: img.convert('RGB')
                     if img.mode != 'RGB' else img),
            T.Resize((self.image_size, self.image_size),
                     interpolation=InterpolationMode.BICUBIC),
            T.ToTensor(),
            T.Normalize(mean=self.IMAGENET_MEAN, std=self.IMAGENET_STD)
        ])

        if not isinstance(data_paths, (list, tuple)):
            data_paths = [data_paths]
        if not isinstance(image_folders, (list, tuple)):
            image_folders = [image_folders]
        if not isinstance(repeat_times, (list, tuple)):
            repeat_times = [repeat_times]
        assert len(data_paths) == len(image_folders) == len(repeat_times)

        print_log('Starting to loading data and calc length', logger='current')
        self.data = []
        self.image_folder = []
        self.group_length = []
        self.conv2length_text = {
        }  # using dict to speedup the calculation of token length

        for data_file, image_folder, repeat_time in zip(
                data_paths, image_folders, repeat_times):
            print_log(
                f'=======Starting to process {data_file} =======',
                logger='current')
            assert repeat_time > 0
            json_data = load_json_or_jsonl(data_file)
            if repeat_time < 1:
                json_data = random.sample(json_data,
                                          int(len(json_data) * repeat_time))
            elif repeat_time > 1:
                int_repeat_time = int(repeat_time)
                remaining_repeat_time = repeat_time - repeat_time
                if remaining_repeat_time > 0:
                    remaining_json_data = random.sample(
                        json_data, int(len(json_data) * remaining_repeat_time))
                    json_data = json_data * int_repeat_time
                    json_data.extend(remaining_json_data)
                else:
                    json_data = json_data * int_repeat_time

            self.data.extend(json_data)
            self.image_folder.extend([image_folder] * len(json_data))

            # TODO: multi process
            for data_item in json_data:
                if 'length' in data_item:
                    token_length = data_item['length']  # include image token
                else:
                    conversations = '\n'.join(
                        [temp['value'] for temp in data_item['conversations']])
                    str_length = len(conversations)

                    if str_length not in self.conv2length_text:
                        token_length = self.tokenizer(
                            conversations,
                            return_tensors='pt',
                            padding=False,
                            truncation=False,
                        ).input_ids.size(1)
                        self.conv2length_text[str_length] = token_length
                    else:
                        token_length = self.conv2length_text[str_length]

                    if 'image' in data_item and data_item['image'] is not None:
                        if 'image_wh' in data_item and data_item[
                                'image_wh'] is not None:
                            # more accurate calculation of image token
                            image_wh = data_item['image_wh']
                            if isinstance(image_wh[0], list):
                                image_wh = image_wh[0]
                            image_token = total_image_token(
                                image_wh, self.min_dynamic_patch,
                                self.max_dynamic_patch, self.image_size,
                                self.use_thumbnail)
                            image_token = self.patch_token * image_token
                        else:
                            # max_dynamic_patch + use_thumbnail
                            image_token = self.patch_token * (
                                self.max_dynamic_patch + self.use_thumbnail)

                        token_length = token_length + image_token
                    else:
                        token_length = -token_length

                self.group_length.append(token_length)
            print_log(
                f'=======total {len(json_data)} samples of {data_file}=======',
                logger='current')

        assert len(self.group_length) == len(self.data)
        print_log('end loading data and calc length', logger='current')
        print_log(
            f'=======total {len(self.data)} samples=======', logger='current')
        self._max_refetch = 1000

    def __getitem__(self, index):
        for _ in range(self._max_refetch + 1):
            data = self.prepare_data(index)
            # Broken images may cause the returned data to be None
            if data is None:
                index = self._rand_another()
                continue
            return data

    def __len__(self):
        return len(self.data)

    @property
    def modality_length(self):
        return self.group_length

    @property
    def length(self):
        group_length = np.array(self.group_length)
        group_length = np.abs(group_length).tolist()
        return group_length

    def prepare_data(self, index):
        data_dict: dict = self.data[index]
        image_folder = self.image_folder[index]

        out_data_dict = {}
        if data_dict.get('image', None) is not None:
            image_wh = data_dict["width"], data_dict["height"]
            # Ensure the first conversation contains an image placeholder
            if '<image>' not in data_dict['conversations'][0]['value']:
                data_dict['conversations'][0]['value'] = \
                    '<image>\n' + data_dict['conversations'][0]['value']
            image_token = total_image_token(
                image_wh, self.min_dynamic_patch,
                self.max_dynamic_patch, self.image_size,
                self.use_thumbnail)
            num_image_tokens = self.patch_token * image_token

            image_token_str = f'{self.IMG_START_TOKEN}' \
                              f'{self.IMG_CONTEXT_TOKEN * num_image_tokens}' \
                              f'{self.IMG_END_TOKEN}'
            token_dict = self.get_inputid_labels(data_dict['conversations'],
                                                 image_token_str)
            out_data_dict['num_patches'] = image_token
            out_data_dict['num_tokens'] = len(token_dict['input_ids'])
            out_data_dict['image_flags'] = torch.tensor(
                [1] * image_token, dtype=torch.long)
        else:
            token_dict = self.get_inputid_labels(data_dict['conversations'],
                                                 None)
            out_data_dict['num_patches'] = 1
            out_data_dict['num_tokens'] = len(token_dict['input_ids'])
            out_data_dict['image_flags'] = torch.tensor([0], dtype=torch.long)
        return out_data_dict

    def _rand_another(self) -> int:
        return np.random.randint(0, len(self.data))

    def get_image(self, path):
        if 's3://' in path:
            img_bytes = get(path)
            with io.BytesIO(img_bytes) as buff:
                img = Image.open(buff).convert('RGB')
            return img
        else:
            return Image.open(path).convert('RGB')

    def get_inputid_labels(self, conversations, image_token_str) -> dict:
        input = ''
        out_conversation = []
        while conversations and conversations[0]['from'] == 'gpt':
            # Skip the first one if it is from gpt
            conversations = conversations[1:]
        for msg in conversations:
            if msg['from'] == 'human':
                if image_token_str is None and '<image>' in msg['value']:
                    warnings.warn(
                        f'The current data << {msg["value"]} >> is '
                        f'in plain text mode, but '
                        'there are <image> tags present in the data. '
                        'We need to remove the <image> tags.')
                    msg['value'] = msg['value'].replace('<image>', '')
                if '<image>' in msg['value']:
                    msg['value'] = msg['value'].replace('<image>', '').strip()
                    msg['value'] = image_token_str + '\n' + msg['value']
                    msg['value'] = msg['value'].strip()
                input += msg['value'].strip()
            elif msg['from'] == 'gpt':
                out_conversation.append({
                    'input': input,
                    'output': msg['value'].strip()
                })
                input = ''
            else:
                raise NotImplementedError

        input_ids, labels = [], []
        for i, single_turn_conversation in enumerate(out_conversation):
            input = single_turn_conversation.get('input', '')
            if input is None:
                input = ''
            input_text = self.template.INSTRUCTION.format(
                input=input, round=i + 1)

            if i == 0:
                system = self.template.SYSTEM.format(system=self._system)
                input_text = system + input_text
                input_encode = self.tokenizer.encode(
                    input_text, add_special_tokens=True)
            else:
                input_encode = self.tokenizer.encode(
                    input_text, add_special_tokens=False)
            input_ids += input_encode
            labels += [IGNORE_INDEX] * len(input_encode)

            output_text = single_turn_conversation.get('output', '')
            if self.template.get('SUFFIX', None):
                output_text += self.template.SUFFIX
            output_encode = self.tokenizer.encode(
                output_text, add_special_tokens=False)
            input_ids += output_encode
            labels += copy.deepcopy(output_encode)

        if len(input_ids) > self.max_length:
            input_ids = input_ids[:self.max_length]
            labels = labels[:self.max_length]
            print_log(
                f'Warning: input_ids length({len(input_ids)}) '
                f'is longer than max_length, cut to {self.max_length}',
                logger='current')
        return {'input_ids': input_ids, 'labels': labels}


def decode_text(args):
    cfg_dataset, inds = args
    dataset = InternVL_V1_5_Dataset(**cfg_dataset)
    dataset.ds_name = "dummy"
    token_lengths = []
    for idx in inds:
        item = dataset.__getitem__(idx)
        flag = item['image_flags'].sum().item()
        if flag == 0:
            num_vit_patch = item['num_patches']
            num_token = item['num_tokens']
            image_flags = 0
        elif flag == -1:
            num_vit_patch = -1
            num_token = -1
            image_flags = -1
        else:
            num_vit_patch = flag
            num_token = item['num_tokens']
            image_flags = flag

        token_lengths.append(
            {
                "vit_num": num_vit_patch,
                "token_num": num_token,
                "image_flags": image_flags
            }
        )

    return token_lengths


def worker(cfg_dataset, ds_name, token_lengths_path, ds_info):
    dataset = InternVL_V1_5_Dataset(**cfg_dataset)
    with multiprocessing.Pool(PROCESSES) as pool:
        token_lengths_all = pool.map(decode_text, [(
            cfg_dataset, inds) for inds in np.array_split(range(len(dataset)), PROCESSES)])
    l_token_lengths = []
    # token_lengths_all = decode_text((cfg_dataset, list(range(len(dataset)))))
    for tmp in token_lengths_all:
        l_token_lengths.extend(tmp)

    length_save_path = os.path.join(
        token_lengths_path, f"{ds_name}"+"_token_lengths.json")

    with open(length_save_path, "w") as f:
        json.dump(l_token_lengths, f, indent=4)
    if "max_dynamic_patch" in ds_info:
        info = {
            "root": ds_info["root"],
            "annotation": ds_info["annotation"],
            "data_augment": ds_info["data_augment"],
            "repeat_time": ds_info["repeat_time"],
            "length": len(dataset),
            "token_lengths": length_save_path,
            "max_dynamic_patch": ds_info["max_dynamic_patch"]
        }
    else:
        info = {
            "root": ds_info["root"],
            "annotation": ds_info["annotation"],
            "data_augment": ds_info["data_augment"],
            "repeat_time": ds_info["repeat_time"],
            "length": len(dataset),
            "token_lengths": length_save_path
        }
    return info


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json_file",
        default=None,
        help="json file to statistics"
    )
    parser.add_argument(
        "--worker",
        default=64, type=int,
        help="worker num",
    )
    parser.add_argument(
        "--token_lengths_path",
        default=None,
        help="token_lengths_path",
    )
    parser.add_argument(
        "--output_path",
        default=None,
        help="token_lengths_path",
    )
    args = parser.parse_args()

    token_lengths_path = args.token_lengths_path

    # setting
    data_path = args.json_file
    from xtuner.utils import PROMPT_TEMPLATE
    cfg_dataset_base = {
        'template': PROMPT_TEMPLATE.internlm2_chat,
        'model_path': '/model/path',
        'max_length': 4096,
    }

    ds_collections = json.loads(open(data_path).read())
    import time
    t_1 = time.time()
    meta = {}
    idx = 0

    datasets = []
    for ds_name in tqdm(ds_collections.keys()):
        print(ds_name)
        cfg_dataset = copy.deepcopy(cfg_dataset_base)
        cfg_dataset['repeat_times'] = ds_collections[ds_name]['repeat_time']
        cfg_dataset['data_paths'] = ds_collections[ds_name]['annotation']
        cfg_dataset['image_folders'] = ds_collections[ds_name]['root']

        ds_info = {}
        ds_info["root"] = ds_collections[ds_name]["root"]
        ds_info["annotation"] = ds_collections[ds_name]["annotation"]
        ds_info["data_augment"] = ds_collections[ds_name].get(
            "data_augment", False)
        ds_info["repeat_time"] = ds_collections[ds_name]['repeat_time']
        if 'max_dynamic_patch' in ds_collections[ds_name]:
            ds_info['max_dynamic_patch'] = ds_collections[ds_name]['max_dynamic_patch']

        meta[ds_name] = worker(cfg_dataset, ds_name,
                               token_lengths_path, ds_info)

    with open(args.output_path, "w") as f:
        json.dump(meta.copy(), f, indent=4)

    t_2 = time.time()
    print(f"time: {t_2-t_1}")
