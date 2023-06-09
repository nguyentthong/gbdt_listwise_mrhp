"""A basic class representing a Dataset."""
import os
import typing
import math
import torch
import matchzoo as mz

import numpy as np
import pandas as pd
from torch.utils import data
from matchzoo.engine.base_callback import BaseCallback
# from collections.abc import Iterable
from matchzoo.helper import logger
from tqdm import tqdm
from tqdm import trange
import copy
import random

def truncate_indices(
    index: typing.Union[int, slice, list],
    length: int
):
    if isinstance(index, int):
        index = [index] if index < length else None
    elif isinstance(index, list):
        index = [i for i in index if i < length]
    else:
        index = None
    return index


class InstanceDataset(data.Dataset):
    """
    Dataset that is built from a data pack.

    :param data_pack: DataPack to build the dataset.
    :param mode: One of "point", "pair", and "list". (default: "point")
    :param num_dup: Number of duplications per instance, only effective when
        `mode` is "pair". (default: 1)
    :param num_neg: Number of negative samples per instance, only effective
        when `mode` is "pair". (default: 1)
    :param batch_size: Batch size. (default: 32)
    :param resample: Either to resample for each epoch, only effective when
        `mode` is "pair". (default: `True`)
    :param shuffle: Either to shuffle the samples/instances. (default: `True`)
    :param sort: Whether to sort data according to length_right. (default: `False`)
    :param callbacks: Callbacks. See `matchzoo.dataloader.callbacks` for more details.

    Examples:
        >>> import matchzoo as mz
        >>> data_pack = mz.datasets.toy.load_data(stage='train')
        >>> preprocessor = mz.preprocessors.BasicPreprocessor()
        >>> data_processed = preprocessor.fit_transform(data_pack)
        >>> dataset_point = mz.dataloader.Dataset(
        ...     data_processed, mode='point', batch_size=32)
        >>> len(dataset_point)
        4
        >>> dataset_pair = mz.dataloader.Dataset(
        ...     data_processed, mode='pair', num_dup=2, num_neg=2, batch_size=32)
        >>> len(dataset_pair)
        1

    """

    def __init__(
        self,
        data_pack: mz.DataPack,
        mode='point',
        num_dup: int = 1,
        num_neg: int = 1,
        resample: bool = False,
        max_pos_samples: int = None,
        shuffle: bool = True,
        sort: bool = False,
        allocate_num: int = None,
        callbacks: typing.List[BaseCallback] = None,
        seed: int = None,
        weighted_sampling: bool = True,
        relation_building_interval: int = 0,
        relation_checkpoint: str = None,
        split: str = 'train',
        list_checkpoint: str = None,
        list_filter: bool = True,
        top_n_for_list: int = 0
    ):
        """Init."""
        if callbacks is None:
            callbacks = []

        if mode not in ('point', 'pair', 'list_train', 'list_test'):
            raise ValueError(f"{mode} is not a valid mode type."
                             f"Must be one of `point`, `pair` or `list`.")

        if shuffle and sort:
            raise ValueError(
                "parameters `shuffle` and `sort` conflict, should not both be `True`.")

        if resample and relation_checkpoint:
            raise ValueError(
                "parameters `resample` and `relation_checkpoint` conflict, use `relation_checkpoint` please set `resample` be `False`.")

        data_pack = data_pack.copy()
        self._mode = mode
        self._num_dup = num_dup
        self._num_neg = num_neg
        self._initial_seed = seed if seed else 1
        self._seed = self._initial_seed
        self._resample = (resample if mode == 'pair' else False)
        self._shuffle = shuffle
        self._sort = sort
        self._allocate_num = allocate_num
        self._orig_relation = data_pack.relation
        self._callbacks = callbacks
        self._instance_index = None
        self._weighted_sampling = weighted_sampling
        self._relation_building_interval = relation_building_interval
        self._use_callbacks = True
        self._max_pos_samples = max_pos_samples
        self._relation_checkpoint = relation_checkpoint
        self._split = split

        self._list_checkpoint = list_checkpoint
        self._list_filter = list_filter
        self._top_n_for_list = top_n_for_list

        if mode == 'pair':
            if not self._relation_checkpoint:
                if weighted_sampling:
                    self._build_sampling_weight()
                data_pack.relation = self._reorganize_pair_wise(
                    relation=self._orig_relation,
                    num_dup=num_dup,
                    num_neg=num_neg
                )
            else:
                data_pack.relation = self._load_pair_wise()
        elif mode == 'list_train' or mode == 'list_test':
            
            if os.path.exists(self._list_checkpoint):
                list_data_pack = torch.load(self._list_checkpoint)
                list_left = list_data_pack['list_left']
                list_right = list_data_pack['list_right']
                list_left_filtered = list_data_pack['list_left_filtered']
                list_right_filtered = list_data_pack['list_right_filtered']
            else:
                merged_data_pack = data_pack.frame
                id_left_to_list_right_index = {}
                index = 0
                list_right = []
                list_left = []

                for i in trange(len(data_pack)):
                    frame = merged_data_pack[i]
                    columns = list(frame.columns)
                    if 'label' in columns:
                        columns.remove('label')
                        label = np.vstack(np.asarray(frame['label']))
                    
                    x = frame[columns].to_dict(orient='list')
                    id_left = x['id_left'][0]
                    text_left = x['text_left'][0]
                    text_left_length = x['text_left_length'][0]
                    id_right = x['id_right'][0]
                    text_right = x['text_right'][0]
                    text_right_length = x['text_right_length'][0]

                    if not id_left in id_left_to_list_right_index:
                        id_left_to_list_right_index[id_left] = index
                        index += 1
                        list_left.append({
                            'right_number': 1,
                            'id_left': [id_left],
                            'text_left': [text_left],
                            'text_left_length': [text_left_length]
                        })
                        list_right.append({
                            'id_right': [id_right],
                            'text_right': [text_right],
                            'text_right_length': [text_right_length],
                            'label': [label]
                        })
                    else:
                        list_right_index = id_left_to_list_right_index[id_left]
                        list_left[list_right_index]['right_number'] += 1
                        list_right[list_right_index]['id_right'].append(id_right)
                        list_right[list_right_index]['text_right'].append(text_right)
                        list_right[list_right_index]['text_right_length'].append(text_right_length)
                        list_right[list_right_index]['label'].append(label)
                
                # sort and filter
                list_left_filtered = []
                list_right_filtered = []

                for i in range(len(list_right)):
                    print('sorting {}'.format(i))
                    current = [[idx, label] for idx, label in enumerate(list_right[i]['label'])]
                    current.sort(key = lambda x: x[1], reverse=True)
                    idx_list = [idx for (idx, label) in current]
                    for k in list_right[i].keys():
                        new = [list_right[i][k][idx] for idx in idx_list]
                        list_right[i][k] = new
                    
                    if self._list_filter:
                        temp = [int(x) for x in list_right[i]['label']]

                        if len(set(temp)) >= 3:
                            list_left_filtered.append(list_left[i])
                            list_right_filtered.append(list_right[i])
                
                list_data_pack = {
                    'list_left': list_left,
                    'list_right': list_right,
                    'list_left_filtered': list_left_filtered,
                    'list_right_filtered': list_right_filtered
                }
                torch.save(list_data_pack, list_checkpoint)
            
            if self._list_filter:
                self._list_left = list_left_filtered
                self._list_right = list_right_filtered
            else:
                self._list_left = list_left
                self._list_right = list_right              


        self._data_pack = data_pack
        self.reset_index()

    def __getitem__(self, item) -> typing.Tuple[dict, np.ndarray]:
        """Get an instance from index idx.

        :param item: the index of the batch.
        """
        if self._mode == 'list_train':
            index = self._instance_index[item]
            review_num_per_list = self.num_neg + 1
            left = self._list_left[index]
            if 'right_number' in left: 
                left.pop('right_number')

            top_n_for_list = self._top_n_for_list
            ori_right = self._list_right[index]
            ori_len_right = len(ori_right['id_right'])
            indices_ori_right = range(ori_len_right)

            if ori_len_right <= review_num_per_list:
                right = copy.deepcopy(ori_right)
                for i in range(review_num_per_list - ori_len_right):
                    sampled = random.sample(indices_ori_right, 1)[0]
                    for k in right.keys():
                        right[k].append(ori_right[k][sampled])
                right['right_valid_list_mask'] = [1]*ori_len_right + [0]*(review_num_per_list - ori_len_right)
            else:
                right = {}
                for k in ori_right.keys():
                    right[k] = []

                for i in range(top_n_for_list):
                    for k in right.keys():
                        right[k].append(ori_right[k][i])
                
                sampled_list = random.sample(indices_ori_right[top_n_for_list:], review_num_per_list - top_n_for_list)
                for sampled in sampled_list:
                    for k in right.keys():
                        right[k].append(ori_right[k][sampled])
                
                right['right_valid_list_mask'] = [1]*review_num_per_list

            for callback in self._callbacks:
                callback.for_list_unpacked(left, right)
            
            return left, right

        elif self._mode == 'list_test':
            index = self._instance_index[item]
            left = self._list_left[index]
            if 'right_number' in left:
                left.pop('right_number')
            
            right = self._list_right[index]
            len_right = len(right['id_right'])
            right['right_valid_list_mask'] = [1]*len_right

            for callback in self._callbacks:
                callback.for_list_unpacked(left, right)
            return left, right
        else:
            indices = self._get_flatten_index(item)
            batch_data_pack = self._data_pack[indices]
            # self._handle_callbacks_on_batch_data_pack(batch_data_pack)
            x, y = batch_data_pack.unpack()

            # TODO: this patch is for deal with loading image callbacks
            if not isinstance(item, slice):
                self._handle_callbacks_on_batch_unpacked(x, y)
            return x, y

    def __len__(self) -> int:
        """Get the total number of batches."""
        return len(self._instance_index)

    def resample_step(self, step):
        """set the random seed and then resampling."""
        # To prevent the same positve instance sampling the same
        # neg instances multiple times.
        self._seed = self._num_dup * step + self._initial_seed
        self.on_epoch_end()

    def on_epoch_end(self):
        """Reorganize the index array if needed."""
        if self._resample:
            self.resample_data()
        self.reset_index()

    def resample_data(self):
        """Reorganize data."""
        if self.mode != 'point':
            self._data_pack.relation = self._reorganize_pair_wise(
                relation=self._orig_relation,
                num_dup=self._num_dup,
                num_neg=self._num_neg
            )

    def reset_index(self):
        """
        Set the :attr:`_batch_indices`.

        Here the :attr:`_batch_indices` records the index of all the instances.
        """
        # index pool: index -> instance index
        if self._mode == 'point':
            step_size = 1
            num_instances = len(self._data_pack)
            index_pool = list(range(num_instances))
        elif self._mode == 'pair':
            step_size = self._num_neg + 1
            num_instances = int(len(self._data_pack) / step_size)
            index_pool = self._split_index_pool(num_instances, step_size)
        elif self._mode == 'list_train' or self._mode == 'list_test':
            # raise NotImplementedError(
            #     f'{self._mode} dataset not implemented.')
            step_size = 1
            num_instances = len(self._list_left)
            index_pool = list(range(num_instances))
        else:
            raise ValueError(f"{self._mode} is not a valid mode type"
                             f"Must be one of `point`, `pair` or `list`.")

        if self._shuffle:
            np.random.seed(self._seed)
            np.random.shuffle(index_pool)

        if self._sort and not 'list' in self._mode:
            old_index_pool = index_pool

            max_instance_right_length = []
            for row in range(len(old_index_pool)):
                instance = self._data_pack[old_index_pool[row]].unpack()[0]
                max_instance_right_length.append(max(instance['length_right']))
            sort_index = np.argsort(max_instance_right_length)
            index_pool = [old_index_pool[index] for index in sort_index]

        if self._allocate_num and not 'list' in self._mode:
            bucket_size = max(math.ceil(self._allocate_num / step_size), 1)
            index_pool = self._get_bucket_indices(index_pool, bucket_size)

        self._instance_index = index_pool

    def _handle_callbacks_on_batch_data_pack(self, batch_data_pack):
        for callback in self._callbacks:
            callback.on_batch_data_pack(batch_data_pack)

    def _handle_callbacks_on_batch_unpacked(self, x, y):
        for callback in self._callbacks:
            callback.on_batch_unpacked(x, y)

    def _split_index_pool(self, num_instances, step_size):
        index_pool = []
        for i in range(num_instances):
            lower = i * step_size
            upper = (i + 1) * step_size
            indices = list(range(lower, upper))
            indices = truncate_indices(indices, len(self._data_pack))
            if indices:
                index_pool.append(indices)
        return index_pool

    def _get_flatten_index(self, item) -> typing.Union[list]:
        if isinstance(item, slice):
            try:
                get_indices = sum(self._instance_index[item], [])
            except:
                get_indices = self._instance_index[item] + []
        else:
            get_indices = self._instance_index[item]

        return get_indices

    def _get_bucket_indices(self, indices, bucket_size):
        allocate_indices = []
        num_instances = len(indices)
        for i in range(math.ceil(num_instances / bucket_size)):
            lower = bucket_size * i
            upper = bucket_size * (i + 1)
            candidates = indices[lower:upper]
            if self._mode == 'pair':
                candidates = sum(candidates, [])
            allocate_indices.append(candidates)
        return allocate_indices

    @property
    def callbacks(self):
        """`callbacks` getter."""
        return self._callbacks

    @callbacks.setter
    def callbacks(self, value):
        """`callbacks` setter."""
        self._callbacks = value

    @property
    def num_neg(self):
        """`num_neg` getter."""
        return self._num_neg

    @num_neg.setter
    def num_neg(self, value):
        """`num_neg` setter."""
        self._num_neg = value
        self.resample_data()
        self.reset_index()

    @property
    def num_dup(self):
        """`num_dup` getter."""
        return self._num_dup

    @num_dup.setter
    def num_dup(self, value):
        """`num_dup` setter."""
        self._num_dup = value
        self.resample_data()
        self.reset_index()

    @property
    def mode(self):
        """`mode` getter."""
        return self._mode

    @property
    def shuffle(self):
        """`shuffle` getter."""
        return self._shuffle

    @shuffle.setter
    def shuffle(self, value):
        """`shuffle` setter."""
        self._shuffle = value
        self.reset_index()

    @property
    def sort(self):
        """`sort` getter."""
        return self._sort

    @sort.setter
    def sort(self, value):
        """`sort` setter."""
        self._sort = value
        self.reset_index()

    @property
    def resample(self):
        """`resample` getter."""
        return self._resample

    @resample.setter
    def resample(self, value):
        """`resample` setter."""
        self._resample = value
        self.reset_index()

    @property
    def instance_index(self):
        """`instance_index` getter."""
        return self._instance_index

    def _build_sampling_weight(self):
        # get weight
        count_dict = self._orig_relation.label.value_counts().to_dict()
        total_num = len(self._orig_relation)
        weight_dict = {k: 1 - v / total_num for k, v in count_dict.items()}

        # set weight
        sampling_weight = self._orig_relation.label.apply(
            lambda x: weight_dict[x])
        self._orig_relation['sampling_weight'] = sampling_weight

    def _reorganize_pair_wise(
        self,
        relation: pd.DataFrame,
        num_dup: int = 1,
        num_neg: int = 1
    ):
        """
        Re-organize the data pack as pair-wise format.

        TODO: Add a weighted sampling strategy.
        """
        logger.info('Sampling reorganize dataset using %d seed...' %
                    self._seed)
        pairs = []
        groups = relation.sort_values(
            'label', ascending=False).groupby('id_left')
        for _, group in tqdm(groups):
            labels = group.label.unique()
            for label in labels[:(-1 - self._relation_building_interval)]:
                pos_samples = group[group.label == label]
                pos_samples = pd.concat([pos_samples] * num_dup)
                neg_samples = group[group.label < (
                    label - self._relation_building_interval)]
                """
                without negative sample for some special condition:

                1) the below sample in the relation building interval
                2) without belowing sample
                """
                if len(neg_samples) == 0:
                    continue

                if self._max_pos_samples:
                    pos_samples = pos_samples.sample(
                        self._max_pos_samples,
                        replace=True,
                        random_state=self._seed
                    )

                for i, pos_sample in pos_samples.iterrows():
                    pos_sample = pd.DataFrame([pos_sample])
                    if self._weighted_sampling:
                        neg_sample = neg_samples.sample(
                            num_neg,
                            replace=True,
                            random_state=self._seed + i,
                            weights='sampling_weight'
                        )
                    else:
                        neg_sample = neg_samples.sample(
                            num_neg,
                            replace=True,
                            random_state=self._seed + i
                        )
                    pairs.extend((pos_sample, neg_sample))

        new_relation = pd.concat(pairs, ignore_index=True)

        # log the sampling result
        logger.info('Sampling relation details:')
        logger.info('Pair number: %d' % (len(new_relation)))
        logger.info_format(new_relation.head((self._num_neg + 1) * 2))
        return new_relation

    def _load_pair_wise(self):
        """
        Load the pair-wise format relation.
        """
        logger.info('Loading pair-wise relation dataset from %s...' %
                    self._relation_checkpoint)
        save_ds = torch.load(self._relation_checkpoint)
        self._mode = save_ds['mode']
        self._num_dup = save_ds['num_dup']
        self._num_neg = save_ds['num_neg']
        self._max_pos_samples = save_ds['max_pos_samples']
        self._relation_building_interval = save_ds['relation_building_interval']
        new_relation = save_ds['relation']

        # log the relation result
        logger.info('Loaded relation details:')
        logger.info('Pair number: %d' % (len(new_relation)))
        logger.info_format(new_relation.head((self._num_neg + 1) * 2))
        return new_relation

    @classmethod
    def generate_dataset(cls, data_pack, mode, num_dup, num_neg, max_pos_samples, building_interval, save_dir, name):
        """
        Bulid a dataset relation
        """
        builded_dataset = cls(
            data_pack,
            mode=mode,
            num_dup=num_dup,
            num_neg=num_neg,
            resample=False,
            relation_building_interval=building_interval,
            max_pos_samples=max_pos_samples)
        
        save_ds = {}
        save_ds['mode'] = mode
        save_ds['num_dup'] = num_dup
        save_ds['num_neg'] = num_neg
        save_ds['max_pos_samples'] = max_pos_samples
        save_ds['relation_building_interval'] = building_interval
        save_ds['relation'] = builded_dataset._data_pack.relation

        path = os.path.join(save_dir, f'{mode}_dup{num_dup}_neg{num_neg}_int{building_interval}.{name}.rel')
        torch.save(save_ds, path)
