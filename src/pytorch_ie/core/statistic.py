import logging
from abc import abstractmethod
from collections import defaultdict
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from pytorch_ie.core.document import Document
from pytorch_ie.core.metric import DocumentMetric
from pytorch_ie.utils.hydra import InstantiationException, resolve_target

logger = logging.getLogger(__name__)


def _flatten_dict_gen(d, parent_key: Tuple[str, ...] = ()) -> Generator:
    for k, v in d.items():
        new_key = parent_key + (k,)
        if isinstance(v, dict):
            yield from dict(_flatten_dict_gen(v, new_key)).items()
        else:
            yield new_key, v


def flatten_dict(d: Dict[str, Any]) -> Dict[Tuple[str, ...], Any]:
    return dict(_flatten_dict_gen(d))


def unflatten_dict(d: Dict[Tuple[str, ...], Any]) -> Union[Dict[str, Any], Any]:
    """Unflattens a dictionary with nested keys.

    Example:
        >>> d = {("a", "b", "c"): 1, ("a", "b", "d"): 2, ("a", "e"): 3}
        >>> unflatten_dict(d)
        {'a': {'b': {'c': 1, 'd': 2}, 'e': 3}}
    """
    result: Dict[str, Any] = {}
    for k, v in d.items():
        if len(k) == 0:
            if len(result) > 1:
                raise ValueError("Cannot unflatten dictionary with multiple root keys.")
            return v
        current = result
        for key in k[:-1]:
            current = current.setdefault(key, {})
        current[k[-1]] = v
    return result


def mean(values: List[float]) -> float:
    return sum(values) / len(values)


def median(values: List[float]) -> float:
    return sorted(values)[len(values) // 2]


def std(values: List[float]) -> float:
    mean_value = mean(values)
    return (sum((x - mean_value) ** 2 for x in values) / len(values)) ** 0.5


def resolve_agg_function(name: str):
    if name == "mean":
        return mean
    elif name == "median":
        return median
    elif name == "std":
        return std
    else:
        try:
            return resolve_target(name)
        except InstantiationException:
            try:
                return resolve_target(f"builtins.{name}")
            except InstantiationException:
                raise ImportError(f"Cannot resolve aggregation function: {name}")


class DocumentStatistic(DocumentMetric):
    """A special type of metric that collects statistics from a document.

    Usage:

    ```python
    from transformers import AutoTokenizer, PreTrainedTokenizer
    from pytorch_ie import DatasetDict
    from pytorch_ie.core import Document, DocumentStatistic

    class TokenCountCollector(DocumentStatistic):

        def __init__(
            self,
            tokenizer: Union[str, PreTrainedTokenizer],
            text_field: str,
            tokenizer_kwargs: Optional[Dict[str, Any]] = None,
            **kwargs,
        ):
            super().__init__(**kwargs)
            self.tokenizer = (
                AutoTokenizer.from_pretrained(tokenizer) if isinstance(tokenizer, str) else tokenizer
            )
            self.tokenizer_kwargs = tokenizer_kwargs or {}
            self.text_field = text_field

        def _collect(self, doc: Document) -> int:
            text = getattr(doc, self.text_field)
            encodings = self.tokenizer(text, **self.tokenizer_kwargs)
            tokens = encodings.tokens()
            return len(tokens)

    dataset = DatasetDict.load_dataset("pie/conll2003")
    statistic = TokenCountCollector(
        text_field="text",
        tokenizer="bert-base-uncased",
        tokenizer_kwargs=dict(add_special_tokens=False),
    )
    values = statistic(dataset)
    assert values == {
        'train': {'mean': 17.950502100989958, 'std': 13.016237876955675, 'min': 1, 'max': 162},
        'validation': {'mean': 19.368307692307692, 'std': 14.583363922289669, 'min': 1, 'max': 144},
        'test': {'mean': 16.774978279756734, 'std': 13.176981022988947, 'min': 1, 'max': 138}
    }
    ```
    """

    DEFAULT_AGGREGATION_FUNCTIONS = ["mean", "std", "min", "max"]

    def __init__(
        self,
        show_histogram: bool = False,
        show_as_markdown: bool = False,
        aggregation_functions: Optional[List[str]] = None,
        title: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.aggregation_functions = {
            f_name: resolve_agg_function(f_name)
            for f_name in aggregation_functions or self.DEFAULT_AGGREGATION_FUNCTIONS
        }
        self.show_histogram = show_histogram
        self.show_as_markdown = show_as_markdown
        self.title = title or self.__class__.__name__

    def reset(self) -> None:
        self._values: List[Any] = []

    @abstractmethod
    def _collect(self, doc: Document) -> Any:
        """Collect any values from a document."""

    def _update(self, document: Document) -> None:
        values = self._collect(document)
        self._values.append(values)

    def _compute(self) -> Any:
        """We just integrate the values by creating lists for each leaf of the (nested)
        dictionary."""
        stats = defaultdict(list)
        for collected_result in self._values:
            if isinstance(collected_result, dict):
                collected_result_flat = flatten_dict(collected_result)
                for k, v in collected_result_flat.items():
                    if isinstance(v, list):
                        stats[k].extend(v)
                    else:
                        stats[k].append(v)
            else:
                if isinstance(collected_result, list):
                    stats[()].extend(collected_result)
                else:
                    stats[()].append(collected_result)
        if self.current_split is not None:
            title = f"{self.title} (split: {self.current_split}, {len(self._values)} documents)"
        else:
            title = f"{self.title} ({len(self._values)} documents)"
        if self.show_histogram:
            import plotext as plt

            for k, values in stats.items():
                if isinstance(values, list):
                    plt.hist(values, label=".".join(k) if len(k) > 0 else None)
            plt.title(title)
            plt.show()
            plt.clear_figure()

        aggregated_stats = {}
        for k, v in stats.items():
            for f_name, f in self.aggregation_functions.items():
                aggregated_stats[k + (f_name,)] = f(v)

        if self.show_as_markdown:
            import pandas as pd

            series = pd.Series(aggregated_stats)
            if len(series.index.levels) > 1:
                df = series.unstack(-1)
                logger.info(f"{title}\n{df.round(3).to_markdown()}")
            else:
                series.index = series.index.get_level_values(0)
                logger.info(f"{title}\n{series.round(3).to_markdown()}")

        return unflatten_dict(aggregated_stats)