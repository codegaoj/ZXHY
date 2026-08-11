from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List

from .models import GraphSample


class SampleLibrary:
    def __init__(self, samples: List[GraphSample]):
        self.samples = samples

    @classmethod
    def load(cls, path: Path) -> "SampleLibrary":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([GraphSample.from_dict(item) for item in raw])

    def tag_counts(self) -> Counter:
        counter = Counter()
        for sample in self.samples:
            counter.update(sample.tags)
        return counter

    def type_counts(self) -> Counter:
        return Counter(sample.sample_type for sample in self.samples)

    def samples_by_tag(self) -> Dict[str, List[GraphSample]]:
        index: Dict[str, List[GraphSample]] = defaultdict(list)
        for sample in self.samples:
            for tag in sample.tags:
                index[tag].append(sample)
        return dict(index)

    def search(self, keyword: str) -> List[GraphSample]:
        keyword = keyword.lower()
        return [
            sample
            for sample in self.samples
            if keyword in sample.title.lower()
            or keyword in sample.sample_type.lower()
            or any(keyword in tag.lower() for tag in sample.tags)
            or keyword in sample.manual_note.lower()
        ]

    def summary(self) -> dict:
        return {
            "sample_count": len(self.samples),
            "type_counts": dict(self.type_counts()),
            "tag_counts": dict(self.tag_counts()),
        }
