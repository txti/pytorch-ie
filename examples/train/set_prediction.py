import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from torch.utils.data import DataLoader

from pytorch_ie.data.datasets.conll2003 import load_conll2003
from pytorch_ie.models.transformer_set_prediction import TransformerSetPredictionModel
from pytorch_ie.taskmodules.transformer_set_prediction import TransformerSetPredictionTaskModule


def main():
    pl.seed_everything(42)

    model_output_path = "./model_output/"
    model_name = "bert-base-cased"
    num_epochs = 10
    batch_size = 16  # tested on a single GeForce RTX 2080 Ti (11016 MB)

    train_docs, val_docs = load_conll2003(split="train"), load_conll2003(split="validation")

    print("train docs: ", len(train_docs))
    print("val docs: ", len(val_docs))

    task_module = TransformerSetPredictionTaskModule(
        tokenizer_name_or_path=model_name,
        max_length=128,
    )

    task_module.prepare(train_docs)

    train_dataset = task_module.encode(train_docs, encode_target=True)
    val_dataset = task_module.encode(val_docs, encode_target=True)

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=task_module.collate,
    )

    val_dataloader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=task_module.collate,
    )

    model = TransformerSetPredictionModel(
        model_name_or_path=model_name,
        num_classes=len(task_module.label_to_id),
        t_total=len(train_dataloader) * num_epochs,
        none_coef=1.0,
        learning_rate=1e-4,
    )

    # checkpoint_callback = ModelCheckpoint(
    #     monitor="val/f1",
    #     dirpath=model_output_path,
    #     filename="zs-ner-{epoch:02d}-val_f1-{val/f1:.2f}",
    #     save_top_k=1,
    #     mode="max",
    #     auto_insert_metric_name=False,
    #     save_weights_only=True,
    # )

    trainer = pl.Trainer(
        fast_dev_run=False,
        max_epochs=num_epochs,
        gpus=1,
        checkpoint_callback=False,
        # callbacks=[checkpoint_callback],
        precision=32,
    )
    trainer.fit(model, train_dataloader, val_dataloader)

    # task_module.save_pretrained(model_output_path)

    # trainer.save_checkpoint(model_output_path + "model.ckpt")
    # or
    # model.save_pretrained(model_output_path)


if __name__ == "__main__":
    main()
