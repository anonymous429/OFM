from functools import wraps
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import copy
import os
import time
import numpy as np
from .utils import EarlyStopping, step_lr, Logger
from .modeling_ofm import OFM
from torch.utils.data import DataLoader

from transformers import TrainingArguments, Trainer
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm


def setup(rank, world_size):
    dist.init_process_group("nccl", rank=rank, world_size=world_size)


def cleanup():
    dist.destroy_process_group()


def get_optimizer_and_scheduler(model, lr):
    # Define the optimizer
    optimizer = AdamW(model.parameters(), lr=lr)

    # Define a custom scheduler
    def lr_lambda(current_step: int):
        # Custom learning rate decay
        return max(0.1, 0.975**current_step)

    scheduler = LambdaLR(optimizer, lr_lambda)

    return optimizer, scheduler


class TrainingArguments:
    def __init__(
        self,
        output_dir,
        per_device_train_batch_size,
        per_device_eval_batch_size,
        evaluation_strategy,
        save_strategy,
        save_total_limit,
        num_train_epochs,
        learning_rate,
        remove_unused_columns,
        push_to_hub,
        report_to,
        label_names,
        fp16,
        weight_decay,
        dataloader_num_workers,
        local_rank,
    ):
        self.output_dir = output_dir
        self.per_device_train_batch_size = per_device_train_batch_size
        self.per_device_eval_batch_size = per_device_eval_batch_size
        self.evaluation_strategy = evaluation_strategy
        self.save_strategy = save_strategy
        self.save_total_limit = save_total_limit
        self.num_train_epochs = num_train_epochs
        self.learning_rate = learning_rate
        self.remove_unused_columns = remove_unused_columns
        self.push_to_hub = push_to_hub
        self.report_to = report_to
        self.label_names = label_names
        self.fp16 = fp16
        self.weight_decay = weight_decay
        self.dataloader_num_workers = dataloader_num_workers
        self.local_rank = local_rank


class Trainer:
    def __init__(
        self,
        supernet: OFM,
        args: TrainingArguments,
        data_collator,
        compute_metrics,
        train_dataset,
        eval_dataset,
        tokenizer,
        optimizers,
    ):
        self.supernet = supernet
        self.avtivate_model = None
        self.args = args
        self.data_collator = data_collator
        self.compute_metrics = compute_metrics
        self.train_dataset = train_dataset
        self.eval_dataset = eval_dataset
        self.tokenizer = tokenizer
        self.optimizer, self.scheduler = optimizers
        self.logger = Logger(log_dir=os.path.join(args.output_dir, "logs"))
        self.train_dataloader = self.get_train_dataloader()
        if self.eval_dataset:
            self.eval_dataloader = self.get_eval_dataloader()
        if self.test_dataset:
            self.test_dataloader = self.get_test_dataloader()

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def get_train_dataloader(self):
        # TODO: remove the unused columns

        return DataLoader(
            self.train_dataset,
            batch_size=self.args.per_device_train_batch_size,
            shuffle=True,
            collate_fn=self.data_collator,
            num_workers=self.args.dataloader_num_workers,
        )

    def get_eval_dataloader(self):

        return DataLoader(
            self.eval_dataset,
            batch_size=self.args.per_device_eval_batch_size,
            shuffle=False,
            collate_fn=self.data_collator,
            num_workers=self.args.dataloader_num_workers,
        )

    def get_test_dataloader(self):

        return DataLoader(
            self.test_dataset,
            batch_size=self.args.per_device_eval_batch_size,
            shuffle=False,
            collate_fn=self.data_collator,
            num_workers=self.args.dataloader_num_workers,
        )

    def create_optimizer_and_scheduler(self):
        self.optimizer = AdamW(
            self.activate_model.parameters(), lr=self.args.learning_rate
        )

        if self.scheduler is None:
            self.scheduler = LambdaLR(
                self.optimizer, lr_lambda=lambda x: max(0.1, 0.975**x)
            )
        else:
            self.scheduler.optimizer = self.optimizer
        # self.scheduler = LambdaLR(self.optimizer, lr_lambda=lambda x: 0.975**x)

    def compute_loss(self, outputs, labels):
        """returns the loss"""

        return outputs.loss

    def _compute_metrics(self, eval_preds):
        if self.compute_metrics is None:
            return {}
        return self.compute_metrics(eval_preds)

    def evaluate(self, eval_dataloader):
        self.activate_model.eval()
        all_preds = []
        all_labels = []
        for batch in eval_dataloader:
            with torch.no_grad():
                outputs = self.model(**batch)
                preds = outputs.logits.argmax(dim=-1)
                all_preds.append(preds)
                all_labels.append(batch["labels"])
        all_preds = torch.cat(all_preds)
        all_labels = torch.cat(all_labels)
        metrics = self._compute_metrics(all_preds, all_labels)
        metrics["params"] = self.avtivate_model.config.num_parameters
        return metrics

    def training_step(self, batch):
        self.activate_model.train()
        outputs = self.activate_model(**batch)
        loss = self.compute_loss(outputs, batch["labels"])
        loss.backward()
        self.optimizer.step()
        self.scheduler.step()

        train_metrics = {
            "train_loss": loss.item(),
            "params": self.activate_model.config.num_parameters,
        }
        return train_metrics

    def train(self):

        for epoch in range(self.args.num_train_epochs):

            for step, batch in enumerate(train_dataloader):
                # move batch to device
                batch = {k: v.to(self.device) for k, v in batch.items()}

                (
                    self.activate_model,
                    self.activate_model.config.num_parameters,
                    self.activate_model.config.arch,
                ) = self.supernet.random_resource_aware_model()

                self.activate_model.to(self.device)

                self.create_optimizer_and_scheduler()

                # print optimizer LR:
                print("the current learning rate")
                print(self.optimizer.param_groups[0]["lr"])

                local_grad = {k: v.cpu() for k, v in ds_model.state_dict().items()}

                train_metrics = self.training_step(batch)

                ds_weights = {k: v.cpu() for k, v in ds_model.state_dict().items()}
                with torch.no_grad():
                    for key in ds_weights:
                        local_grad[key] = local_grad[key] - ds_weights[key]

                self.logger.log_metrics(train_metrics, step, prefix="steps")
                self.logger.print_metrics(train_metrics, step, prefix="steps")
                if step % 100 == 0:
                    metrics = self.evaluate(eval_dataloader)
                    self.logger.log_metrics(metrics, step, prefix="steps")
                    self.logger.print_metrics(metrics, step, prefix="steps")

                    self.save_metrics("eval" + f"-step {step}", metrics)

                self.supernet.apply_grad(local_grad)
                self.supernet.save_ckpt(os.path.join(args.save_dir, "last_model"))

        return metrics

    def distribute_training_step(self, batch):
        self.activate_model.train()
        outputs = self.activate_model(**batch)
        loss = self.compute_loss(outputs, batch["labels"])
        loss.backward()
        for param in self.activate_model.parameters():
            dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
            param.grad.data /= dist.get_world_size()
        self.optimizer.step()
        self.scheduler.step()
        return loss

    def distribute_train(self, rank, world_size):
        setup(rank, world_size)
        os.environ["CUDA_VISIBLE_DEVICES"] = str(rank)
        self.activate_model = self.supernet.random_resource_aware_model()
        self.create_optimizer_and_scheduler()

        for epoch in range(self.args.num_train_epochs):
            for step, batch in enumerate(self.train_dataloader):
                loss = self.training_step(batch)
                self.logger.log_metrics(
                    {"train_loss": loss.item()},
                    step,
                    prefix="steps",
                )
                if step % 100 == 0:
                    metrics = self.evaluate(eval_dataloader)
                    self.logger.log_metrics(
                        metrics,
                        step,
                        prefix="steps",
                    )
                    self.save_metrics("eval" + f"-step {step}", metrics)

        cleanup()
