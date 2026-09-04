"""UI perception: normalize multi-window a11y trees and package observations."""

from perception.filters import ConciseFilter, DetailedFilter, TreeFilter
from perception.normalizer import (
    normalize_a11y_tree,
    render_semantic_tree,
)
from perception.observation import ObservationBuilder, ObservationPackage
from perception.som import render_som
from perception.uiautomator import parse_uiautomator_xml

__all__ = [
    "ConciseFilter",
    "DetailedFilter",
    "ObservationBuilder",
    "ObservationPackage",
    "TreeFilter",
    "normalize_a11y_tree",
    "parse_uiautomator_xml",
    "render_semantic_tree",
    "render_som",
]
