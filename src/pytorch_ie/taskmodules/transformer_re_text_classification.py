import json
import logging
from collections import Counter, defaultdict
from typing import (
    Any,
    DefaultDict,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    Union,
)

import numpy as np
import torch
from transformers import AutoTokenizer
from transformers.file_utils import PaddingStrategy
from transformers.tokenization_utils_base import BatchEncoding, TruncationStrategy

from pytorch_ie.data.document import Annotation, BinaryRelation, Document, LabeledSpan
from pytorch_ie.models import (
    TransformerTextClassificationModelBatchOutput,
    TransformerTextClassificationModelStepBatchEncoding,
)
from pytorch_ie.taskmodules.taskmodule import Metadata, TaskEncoding, TaskModule
from pytorch_ie.utils.span import get_token_slice, is_contained_in
from pytorch_ie.utils.window import get_window_around_slice

"""
workflow:
    Document
        -> (InputEncoding, TargetEncoding) -> TaskEncoding -> TaskBatchEncoding
            -> ModelBatchEncoding -> ModelBatchOutput
        -> TaskOutput
    -> Document
"""

TransformerReTextClassificationInputEncoding = Dict[str, Any]
TransformerReTextClassificationTargetEncoding = List[int]

TransformerReTextClassificationTaskEncoding = TaskEncoding[
    TransformerReTextClassificationInputEncoding, TransformerReTextClassificationTargetEncoding
]


class TransformerReTextClassificationTaskOutput(TypedDict, total=False):
    labels: List[str]
    probabilities: List[float]


_TransformerReTextClassificationTaskModule = TaskModule[
    # _InputEncoding, _TargetEncoding, _TaskBatchEncoding, _ModelBatchOutput, _TaskOutput
    TransformerReTextClassificationInputEncoding,
    TransformerReTextClassificationTargetEncoding,
    TransformerTextClassificationModelStepBatchEncoding,
    TransformerTextClassificationModelBatchOutput,
    TransformerReTextClassificationTaskOutput,
]

HEAD = "head"
TAIL = "tail"
START = "start"
END = "end"


logger = logging.getLogger(__name__)


def _create_argument_markers(
    entity_labels: List[str], add_type_to_marker: bool
) -> Dict[Union[Tuple[str, str, str], Tuple[str, str]], str]:
    argument_markers: Dict[Union[Tuple[str, str, str], Tuple[str, str]], str] = {}
    for arg_type in [HEAD, TAIL]:
        is_head = arg_type == HEAD

        for arg_pos in [START, END]:
            is_start = arg_pos == START

            if add_type_to_marker:
                for entity_type in entity_labels:
                    marker = f"[{'' if is_start else '/'}{'H' if is_head else 'T'}:{entity_type}]"
                    argument_markers[(arg_type, arg_pos, entity_type)] = marker
            else:
                marker = f"[{'' if is_start else '/'}{'H' if is_head else 'T'}]"
                argument_markers[(arg_type, arg_pos)] = marker

    return argument_markers


def _enumerate_entity_pairs(
    entities: List[LabeledSpan],
    partition: Optional[LabeledSpan] = None,
    relations: List[BinaryRelation] = None,
):
    """
    Given a list of `entities` iterate all valid pairs of entities. If a `partition` is provided,
    restrict pairs to be contained in that. If `relations` are given, return only pairs for which a relation exists.
    """
    existing_head_tail = {(relation.head, relation.tail) for relation in relations or []}
    head: LabeledSpan
    for head in entities:
        if partition is not None and not is_contained_in(
            (head.start, head.end), (partition.start, partition.end)
        ):
            continue

        tail: LabeledSpan
        for tail in entities:
            if partition is not None and not is_contained_in(
                (tail.start, tail.end), (partition.start, partition.end)
            ):
                continue

            if head == tail:
                continue

            if relations is not None and (head, tail) not in existing_head_tail:
                continue

            yield head, tail


class TransformerRETextClassificationTaskModule(_TransformerReTextClassificationTaskModule):
    """
    Marker based relation extraction. This taskmodule prepares the input token ids in such a way
    that before and after the candidate head and tail entities special marker tokens are inserted.
    Then, the modified token ids can be simply passed into a transformer based text classifier model.

    parameters:

        partition_annotation: str, optional. If specified, LabeledSpan annotations with this name are
            expected to define partitions of the document that will be processed individually, e.g. sentences
            or sections of the document text.
        none_label: str, defaults to "no_relation". The relation label that indicate dummy/negative relations.
            Predicted relations with that label will not be added to the document(s).
        max_window: int, optional. If specified, use the tokens in a window of maximal this amount of tokens
            around the center of head and tail entities and pass only that into the transformer.

        TODO: add remaining parameters
    """

    def __init__(
        self,
        tokenizer_name_or_path: str,
        entity_annotation: str = "entities",
        relation_annotation: str = "relations",
        partition_annotation: Optional[str] = None,
        none_label: str = "no_relation",
        padding: Union[bool, str, PaddingStrategy] = True,
        truncation: Union[bool, str, TruncationStrategy] = True,
        max_length: Optional[int] = None,
        pad_to_multiple_of: Optional[int] = None,
        multi_label: bool = False,
        label_to_id: Optional[Dict[str, int]] = None,
        add_type_to_marker: bool = False,
        single_argument_pair: bool = True,
        append_markers: bool = False,
        entity_labels: Optional[List[str]] = None,
        max_window: Optional[int] = None,
        show_statistics: bool = False,
    ) -> None:
        super().__init__()
        self.save_hyperparameters()

        self.entity_annotation = entity_annotation
        self.relation_annotation = relation_annotation
        self.padding = padding
        self.truncation = truncation
        self.label_to_id = label_to_id or {}
        self.id_to_label = {v: k for k, v in self.label_to_id.items()}
        self.max_length = max_length
        self.pad_to_multiple_of = pad_to_multiple_of
        self.multi_label = multi_label
        self.add_type_to_marker = add_type_to_marker
        self.single_argument_pair = single_argument_pair
        self.append_markers = append_markers
        self.entity_labels = entity_labels
        self.partition_annotation = partition_annotation
        self.none_label = none_label
        self.max_window = max_window
        self.show_statistics = show_statistics

        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_name_or_path)

        self.argument_markers = None

        if self.is_prepared():
            self.argument_markers = _create_argument_markers(
                # ignore typing because is_prepared already checks that entity_labels is not None
                entity_labels=self.entity_labels,  # type: ignore
                add_type_to_marker=self.add_type_to_marker,
            )
            # do not sort here to keep order from loaded taskmodule config
            self.tokenizer.add_tokens(list(self.argument_markers.values()), special_tokens=True)

    def _config(self) -> Dict[str, Any]:
        config = super()._config()
        config["label_to_id"] = self.label_to_id
        config["entity_labels"] = self.entity_labels
        return config

    def is_prepared(self):
        """
        This should return True iff all config entries added by the _config() method are available.
        By doing so, it marks the taskmodule ready to save with save_pretrained(), i.e. that the
        exact same taskmodule will be produced when loaded again via from_pretrained().
        """
        return self.entity_labels is not None and self.label_to_id is not None

    def prepare(self, documents: List[Document]) -> None:
        entity_labels = set()
        relation_labels = set()
        for document in documents:
            entities = document.span_annotations(self.entity_annotation)
            relations = document.relation_annotations(self.relation_annotation)
            assert (
                entities is not None
            ), f"document has no span annotations with name '{self.entity_annotation}'"
            assert (
                relations is not None
            ), f"document has no relation annotations with name '{self.relation_annotation}'"

            if self.add_type_to_marker:
                for entity in entities:
                    entity_labels.update(entity.labels)

            for relation in relations:
                relation_labels.update(relation.labels)

        if self.none_label in relation_labels:
            relation_labels.remove(self.none_label)

        self.label_to_id = {label: i + 1 for i, label in enumerate(sorted(relation_labels))}
        self.label_to_id[self.none_label] = 0

        self.id_to_label = {v: k for k, v in self.label_to_id.items()}

        self.entity_labels = sorted(entity_labels)
        argument_markers = _create_argument_markers(
            entity_labels=self.entity_labels, add_type_to_marker=self.add_type_to_marker
        )
        # Sort argument markers by value to ensure that added tokens are in a reproducible order.
        # Note: To maintain backwards compatibility, the argument markers are not sorted when loading from a saved
        # taskmodule!
        self.argument_markers = dict(sorted(argument_markers.items(), key=lambda kv: kv[1]))
        self.tokenizer.add_tokens(list(self.argument_markers.values()), special_tokens=True)

    def _encode_text(
        self,
        document: Document,
        partition: Optional[LabeledSpan] = None,
        add_special_tokens: bool = True,
    ) -> BatchEncoding:
        text = (
            document.text[partition.start : partition.end]
            if partition is not None
            else document.text
        )
        encoding = self.tokenizer(
            text,
            padding=False,
            truncation=self.truncation,
            max_length=self.max_length,
            is_split_into_words=False,
            return_offsets_mapping=False,
            add_special_tokens=add_special_tokens,
        )
        return encoding

    def encode_input(
        self, documents: List[Document]
    ) -> Tuple[
        List[TransformerReTextClassificationInputEncoding],
        List[Metadata],
        Optional[List[Document]],
    ]:
        assert (
            self.argument_markers is not None
        ), f"No argument markers available, was `prepare` already called?"
        argument_markers_to_id = {
            marker: self.tokenizer.vocab[marker] for marker in self.argument_markers.values()
        }
        input_encoding = []
        metadata = []
        new_documents = []
        statistics: Optional[DefaultDict[str, Counter]] = (
            defaultdict(Counter) if self.show_statistics else None
        )

        for document in documents:
            entities = document.span_annotations(self.entity_annotation)
            assert (
                entities is not None
            ), f"document has no span annotations of name {self.entity_annotation}"
            relations = document.relation_annotations(self.relation_annotation)
            relation_mapping = {(rel.head, rel.tail): rel.label for rel in relations or []}

            partitions: Sequence[Optional[LabeledSpan]]
            if self.partition_annotation is not None:
                partitions_or_none = document.span_annotations(self.partition_annotation)
                assert (
                    partitions_or_none is not None
                ), f"document has no span annotations of name {self.partition_annotation}"
                partitions = partitions_or_none
            else:
                # use single dummy partition
                partitions = [None]

            for partition_idx, partition in enumerate(partitions):
                partition_offset = 0 if partition is None else partition.start
                add_special_tokens = self.max_window is None
                encoding = self._encode_text(
                    document=document, partition=partition, add_special_tokens=add_special_tokens
                )

                for head, tail, in _enumerate_entity_pairs(
                    entities=entities,
                    partition=partition,
                    relations=relations,
                ):
                    head_token_slice = get_token_slice(
                        character_slice=(head.start, head.end),
                        char_to_token_mapper=encoding.char_to_token,
                        character_offset=partition_offset,
                    )
                    tail_token_slice = get_token_slice(
                        character_slice=(tail.start, tail.end),
                        char_to_token_mapper=encoding.char_to_token,
                        character_offset=partition_offset,
                    )
                    # this happens if the head/tail start/end does not match a token start/end
                    if head_token_slice is None or tail_token_slice is None:
                        if statistics is not None:
                            statistics["entity_token_alignment_error"][
                                relation_mapping.get((head, tail), "TO_PREDICT")
                            ] += 1
                        continue

                    input_ids = encoding["input_ids"]

                    # windowing
                    if self.max_window is not None:
                        head_start, head_end = head_token_slice
                        tail_start, tail_end = tail_token_slice
                        # The actual number of tokens will be lower than max_window because we add the
                        # 4 marker tokens (before / after the head /tail) and the default special tokens
                        # (e.g. CLS and SEP).
                        num_added_special_tokens = len(
                            self.tokenizer.build_inputs_with_special_tokens([])
                        )
                        max_tokens = self.max_window - 4 - num_added_special_tokens
                        # the slice from the beginning of the first entity to the end of the second is required
                        slice_required = (min(head_start, tail_start), max(head_end, tail_end))
                        window_slice = get_window_around_slice(
                            slice=slice_required,
                            max_window_size=max_tokens,
                            available_input_length=len(input_ids),
                        )
                        # this happens if slice_required does not fit into max_tokens
                        if window_slice is None:
                            if statistics is not None:
                                statistics["out_of_token_window"][
                                    relation_mapping.get((head, tail), "TO_PREDICT")
                                ] += 1
                            continue

                        window_start, window_end = window_slice
                        input_ids = input_ids[window_start:window_end]

                        head_token_slice = head_start - window_start, head_end - window_start
                        tail_token_slice = tail_start - window_start, tail_end - window_start

                    if head_token_slice[0] < tail_token_slice[0]:
                        assert (
                            head_token_slice[1] <= tail_token_slice[0]
                        ), f"the head and tail entities are not allowed to overlap"
                        entity_pair = (head, tail)
                        entity_slices = (head_token_slice, tail_token_slice)
                        entity_args = (HEAD, TAIL)
                    else:
                        assert (
                            tail_token_slice[1] <= head_token_slice[0]
                        ), f"the head and tail entities are not allowed to overlap"
                        entity_pair = (tail, head)
                        entity_slices = (tail_token_slice, head_token_slice)
                        entity_args = (TAIL, HEAD)

                    markers = {}
                    for entity, arg_name in zip(entity_pair, entity_args):
                        for pos in [START, END]:
                            if self.add_type_to_marker:
                                if entity.is_multilabel:
                                    raise NotImplementedError
                                markers[(arg_name, pos)] = argument_markers_to_id[
                                    self.argument_markers[(arg_name, pos, entity.label_single)]
                                ]
                            else:
                                markers[(arg_name, pos)] = argument_markers_to_id[
                                    self.argument_markers[(arg_name, pos)]
                                ]

                    new_input_ids = (
                        input_ids[: entity_slices[0][0]]
                        + [markers[(entity_args[0], START)]]
                        + input_ids[entity_slices[0][0] : entity_slices[0][1]]
                        + [markers[(entity_args[0], END)]]
                        + input_ids[entity_slices[0][1] : entity_slices[1][0]]
                        + [markers[(entity_args[1], START)]]
                        + input_ids[entity_slices[1][0] : entity_slices[1][1]]
                        + [markers[(entity_args[1], END)]]
                        + input_ids[entity_slices[1][1] :]
                    )

                    # when windowing is used, we have to add the special tokens manually
                    if not add_special_tokens:
                        new_input_ids = self.tokenizer.build_inputs_with_special_tokens(
                            token_ids_0=new_input_ids
                        )

                    input_encoding.append({"input_ids": new_input_ids})
                    new_documents.append(document)
                    doc_metadata = {
                        HEAD: head,
                        TAIL: tail,
                    }
                    metadata.append(doc_metadata)
                    if statistics is not None:
                        statistics["candidates"][
                            relation_mapping.get((head, tail), "TO_PREDICT")
                        ] += 1

        if statistics is not None:
            logger.info(f"statistics:\n{json.dumps(statistics, indent=2)}")
        return input_encoding, metadata, new_documents

    def encode_target(
        self,
        documents: List[Document],
        input_encodings: List[TransformerReTextClassificationInputEncoding],
        metadata: List[Metadata],
    ) -> List[TransformerReTextClassificationTargetEncoding]:

        target: List[TransformerReTextClassificationTargetEncoding] = []
        for i, document in enumerate(documents):
            meta = metadata[i]

            relations = document.relation_annotations(self.relation_annotation)
            assert (
                relations is not None
            ), f"document has no relation annotations of name '{self.relation_annotation}'"

            head_tail_to_labels = {
                (relation.head, relation.tail): relation.labels for relation in relations
            }

            labels = head_tail_to_labels.get((meta[HEAD], meta[TAIL]), [self.none_label])
            label_ids = [self.label_to_id[label] for label in labels]
            target.append(label_ids)

        return target

    def unbatch_output(
        self, output: TransformerTextClassificationModelBatchOutput
    ) -> Sequence[TransformerReTextClassificationTaskOutput]:
        logits = output["logits"]

        output_label_probs = logits.sigmoid() if self.multi_label else logits.softmax(dim=-1)
        output_label_probs = output_label_probs.detach().cpu().numpy()

        decoded_output = []
        if self.multi_label:
            raise NotImplementedError
        else:
            label_ids = np.argmax(output_label_probs, axis=-1)
            for batch_idx, label_id in enumerate(label_ids):
                label = self.id_to_label[label_id]
                prob = float(output_label_probs[batch_idx, label_id])
                result: TransformerReTextClassificationTaskOutput = {
                    "labels": [label],
                    "probabilities": [prob],
                }
                decoded_output.append(result)

        return decoded_output

    def create_annotations_from_output(
        self,
        encoding: TransformerReTextClassificationTaskEncoding,
        output: TransformerReTextClassificationTaskOutput,
    ) -> Iterator[Tuple[str, Annotation]]:
        labels = output["labels"]
        probabilities = output["probabilities"]
        if labels != [self.none_label]:
            yield (
                self.relation_annotation,
                BinaryRelation(
                    head=encoding.metadata[HEAD],
                    tail=encoding.metadata[TAIL],
                    label=labels if self.multi_label else labels[0],
                    score=probabilities if self.multi_label else probabilities[0],
                ),
            )

    def collate(
        self, encodings: List[TransformerReTextClassificationTaskEncoding]
    ) -> TransformerTextClassificationModelStepBatchEncoding:

        input_features = [encoding.input for encoding in encodings]

        input_ = self.tokenizer.pad(
            input_features,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors="pt",
        )

        if not encodings[0].has_target:
            return input_, None

        target_list: List[TransformerReTextClassificationTargetEncoding] = [
            encoding.target for encoding in encodings
        ]
        target = torch.tensor(target_list, dtype=torch.int64)

        if not self.multi_label:
            target = target.flatten()

        return input_, target