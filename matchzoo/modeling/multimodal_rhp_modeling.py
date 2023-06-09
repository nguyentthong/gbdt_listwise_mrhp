import os
import matchzoo as mz

from collections import defaultdict
from matchzoo.helper import logger
from matchzoo.modeling import BasicModeling
from matchzoo.dataloader import InstanceDataset, InstanceDataLoader

from matchzoo.models.multimodal_rhp import (
    CommonSpaceMultimodalLayernormRHPNet3
)
from matchzoo.dataloader.callbacks.padding import TextFieldPadding, ImageFieldPadding
from matchzoo.dataloader.callbacks.load_img import LoadImage
from matchzoo.pipeline.rhp_pipeline import RHPPipeline
from matchzoo.trainers.modeling_trainer import ModelingTrainer


class MultimodalRHP(BasicModeling):
    '''
    A modeling implementation for Multimodal Review Helpfulness Prediction task.
    '''

    def build_metrics(self):
        threshold = self.config.test.get('threshold', 0)
        self.metrics = [
            mz.metrics.MeanReciprocalRank(threshold=threshold),
            mz.metrics.MeanAveragePrecision(threshold=threshold),
            mz.metrics.NormalizedDiscountedCumulativeGain(
                k=3, threshold=threshold),
            mz.metrics.NormalizedDiscountedCumulativeGain(
                k=5, threshold=threshold)
        ]

    def build_preprocessor(self):
        self.preprocessor = RHPPipeline(
            language=self.config.data.language,
            prd_filter_low_freq=self.config.preprocess.prd_filter_low_freq,
            rvw_filter_low_freq=self.config.preprocess.rvw_filter_low_freq
        )

    def load_data(self, dataclass):
        train_pack = dataclass.load_data(
            read_type=self.config.data.read_type,
            feature_root=self.config.data.feat_dir,
            cat=self.config.data.cat,
            data_root=self.config.data.data_dir,
            stage='train',
            task=self.task
        )

        dev_pack = dataclass.load_data(
            read_type=self.config.data.read_type,
            feature_root=self.config.data.feat_dir,
            cat=self.config.data.cat,
            data_root=self.config.data.data_dir,
            stage='dev',
            task=self.task
        )

        test_pack = dataclass.load_data(
            read_type=self.config.data.read_type,
            feature_root=self.config.data.feat_dir,
            cat=self.config.data.cat,
            data_root=self.config.data.data_dir,
            stage='test',
            task=self.task
        )

        return (train_pack, dev_pack, test_pack)

    def build_dataset_callback(self):
        if self.config.input_setting.use_img:
            self.dataset_callback = defaultdict(list)
            for k in self.datapack:
                image_loading = LoadImage(
                    feat_dir=os.path.join(self.config.data.feat_dir, k),
                    feat_size=self.config.prd_img_encoder.input_dim,
                    max_roi_num=self.config.input_setting.max_roi_per_img,
                    img_min_length=self.config.input_setting.img_min_length
                )
                self.dataset_callback[k].append(image_loading)
        else:
            self.dataset_callback = {}

    def build_dataset(self):
        for k, v in self.datapack.items():
            if v is None:
                continue

            if k == 'train':
                logger.info('Build the %s dataset with %d batch size...' % (
                    k, self.config.train.batch_size * self.config.train.allocate_num
                )
                )
                dataset = InstanceDataset(
                    data_pack=self.datapack[k],
                    mode=self.config.train.mode,
                    num_dup=self.config.train.num_dup,
                    num_neg=self.config.train.num_neg,
                    shuffle=self.config.train.shuffle,
                    allocate_num=self.config.train.allocate_num,
                    resample=self.config.train.resample,
                    callbacks=self.dataset_callback.get(k, None),
                    weighted_sampling=self.config.train.weighted_sampling,
                    relation_building_interval=self.config.train.relation_building_interval,
                    relation_checkpoint=self.config.train.relation_checkpoint if self.config.train.contains('relation_checkpoint') else None,
                    split=k,
                    list_checkpoint=k + '_' + self.config.data.list_checkpoint,
                    list_filter=True
                )
            else:
                logger.info('Build the %s dataset with %d batch size...' % (
                    k, self.config.test.batch_size * self.config.test.allocate_num
                )
                )
                dataset = InstanceDataset(
                    data_pack=self.datapack[k],
                    shuffle=False,
                    callbacks=self.dataset_callback.get(k, None),
                    mode=self.config.test.mode,
                    split=k,
                    list_checkpoint=k + '_' + self.config.data.list_checkpoint,
                    list_filter=False
                )
            self.dataset[k] = dataset

    def build_dataloader_callback(self):
        callbacks = []
        text_padding = TextFieldPadding(
            text_fields=['text_left', 'text_right'],
            fixed_length=None,
            max_length=self.config.input_setting.txt_max_length,
            min_length=self.config.input_setting.txt_min_length,
            pad_word_value=[
                self.preprocessor.prd_text_field.vocab.pad_index,
                self.preprocessor.rvw_text_field.vocab.pad_index
            ],
            pad_word_mode='post'
        )
        callbacks.append(text_padding)
        if self.config.input_setting.use_img:
            image_padding = ImageFieldPadding(
                image_fields=['image_left', 'image_right'],
                max_roi_per_inst=self.config.input_setting.max_roi_per_img,
                feat_size=self.config.prd_img_encoder.input_dim,
                fixed_length=None,
                max_length=self.config.input_setting.img_max_length,
                min_length=self.config.input_setting.img_min_length,
                pad_word_mode='post'
            )
            callbacks.append(image_padding)

        self.dataloader_callback = callbacks


class BigDataMultimodalRHP(MultimodalRHP):
    '''
    A modeling implementation for Multimodal Review Helpfulness Prediction task with sampling data.
    '''
    def build_dataset(self):
        for k, v in self.datapack.items():
            if v is None:
                continue

            if k == 'train':
                logger.info('Build the %s dataset with %d batch size...' % (
                    k, self.config.train.batch_size * self.config.train.allocate_num
                )
                )
                dataset = InstanceDataset(
                    data_pack=self.datapack[k],
                    mode=self.config.train.mode,
                    num_dup=self.config.train.num_dup,
                    num_neg=self.config.train.num_neg,
                    shuffle=self.config.train.shuffle,
                    allocate_num=self.config.train.allocate_num,
                    resample=self.config.train.resample,
                    callbacks=self.dataset_callback.get(k, None),
                    weighted_sampling=self.config.train.weighted_sampling,
                    relation_building_interval=self.config.train.relation_building_interval,
                    relation_checkpoint=self.config.train.relation_checkpoint if self.config.train.contains('relation_checkpoint') else None
                )
            else:
                # in order to quickly evaluate the method when using `dev` stage
                if k == 'dev':
                    dataset = InstanceDataset(
                        data_pack=self.datapack[k],
                        mode='pair',
                        num_dup=self.config.eval.num_dup,
                        num_neg=self.config.eval.num_neg,
                        max_pos_samples=self.config.eval.max_pos_samples,
                        shuffle=False,
                        allocate_num=self.config.eval.allocate_num,
                        callbacks=self.dataset_callback.get(k, None),
                        resample=self.config.eval.resample,
                        weighted_sampling=self.config.eval.weighted_sampling,
                        relation_building_interval=self.config.eval.relation_building_interval
                    )
                else:
                    dataset = InstanceDataset(
                        data_pack=self.datapack[k],
                        mode='point',
                        shuffle=False,
                        allocate_num=self.config.test.allocate_num,
                        callbacks=self.dataset_callback.get(k, None)
                    )
            self.dataset[k] = dataset


class CommonSpaceMultimodalLayernormRHP3(MultimodalRHP):
    def build_model(self):
        self.model = CommonSpaceMultimodalLayernormRHPNet3(
            self.config, self.preprocessor, self.stage)


class BigDataCommonSpaceMultimodalLayernormRHP3(BigDataMultimodalRHP):
    def build_model(self):
        self.model = CommonSpaceMultimodalLayernormRHPNet3(
            self.config, self.preprocessor, self.stage)


class ListWiseCommonSpaceMultimodalLayernormRHP3(MultimodalRHP):
    def build_model(self):
        self.model = CommonSpaceMultimodalLayernormRHPNet3(self.config, self.preprocessor, self.stage)

    def build_dataloader(self):
        train_batch_size = self.config.train.batch_size
        test_batch_size = self.config.test.batch_size
        worker = 0

        for k, v in self.dataset.items():
            if v is None:
                continue
            logger.info("Build the {} dataloader with {} worker...".format(k, worker))

            if k == 'train':
                self.dataloader[k] = InstanceDataLoader(
                    dataset=v,
                    if_list=True if self.config.train.mode == 'list_train' else False,
                    batch_size=train_batch_size,
                    rank=self.rank,
                    sampler=self.dataset_sampler[k],
                    stage=k,
                    num_workers=worker,
                    callbacks=self.dataloader_callback
                )
            else:
                self.dataloader[k] = InstanceDataLoader(
                    dataset=v,
                    if_list=True if self.config.test.mode == 'list_test' else False,
                    batch_size=1 if self.config.test.mode == 'list_test' else test_batch_size,
                    rank=self.rank,
                    sampler=self.dataset_sampler[k],
                    stage=k,
                    num_workers=worker,
                    callbacks=self.dataloader_callback
                )

    def build_trainer(self):
        trainer = ModelingTrainer(
            config=self.config,
            model=self.model,
            task=self.task,
            optimizer=self.optimizer,
            main_metric=self.config.train.main_metric,
            scheduler=self.scheduler,
            trainloader=self.dataloader['train'],
            validloader=self.dataloader['dev'],
            validate_interval=self.config.train.validate_interval,
            validate_at_epoch_end=self.config.train.validate_at_epoch_end,
            save_interval=self.config.train.save_interval,
            start_epoch=self.config.train.start_epoch,
            epochs=self.config.train.end_epoch,
            device=self.device,
            stage=self.stage,
            patience=self.config.train.early_stopping,
            save_dir=self.config.train.checkpoint.dir,
            save_all=True,
            checkpoint=self.ckpt,
            debug=self.config.log.debug,
            train_mode=self.config.train.mode,
            test_mode=self.config.test.mode
        )
        self.trainer = trainer