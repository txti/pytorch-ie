import abc
from typing import Mapping, Optional, Type

import datasets
from datasets.load import load_dataset_builder

from pytorch_ie.data.dataset import Dataset, decorate_convert_to_dict_of_lists
from pytorch_ie.document import Document


class GeneratorBasedBuilder(datasets.builder.GeneratorBasedBuilder):
    DOCUMENT_TYPE: Optional[Type[Document]] = None

    BASE_PATH: Optional[str] = None

    def __init__(self, **kwargs):
        builder_kwargs = dict(kwargs)
        builder_kwargs.pop("hash", None)
        builder_kwargs.pop("base_path", None)
        self.base_builder = load_dataset_builder(
            path=self.BASE_PATH,
            **builder_kwargs,
        )
        super().__init__(**kwargs)

    def _info(self):
        return self.base_builder._info()

    def _split_generators(self, dl_manager):
        return self.base_builder._split_generators(dl_manager)

    def _generate_examples(self, filepath):
        return self.base_builder._generate_examples(filepath)

    @abc.abstractmethod
    def _generate_document(self, example, dataset):
        pass

    def _generate_document_kwargs(self, dataset):
        return None

    def _post_process(
        self, dataset: datasets.Dataset, resources_paths: Mapping[str, str]
    ) -> Optional[datasets.Dataset]:
        fn_kwargs = {}
        additional_kwargs = self._generate_document_kwargs(dataset)

        if additional_kwargs is not None:
            fn_kwargs.update(additional_kwargs)

        mapped_dataset = dataset.map(
            decorate_convert_to_dict_of_lists(self._generate_document), fn_kwargs=fn_kwargs
        )

        document_dataset = Dataset.from_hf_dataset(
            mapped_dataset, document_type=self.DOCUMENT_TYPE
        )

        return document_dataset