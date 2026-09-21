"""Palette coverage, compact output and index-free comparison regressions."""
from io import BytesIO

import pytest
from PIL import Image

from agent.read_tools import make_inspect_image_regions_handler
from agent.tool_registry import AgentRole, ToolExecutionContext
from perception.observation import ObservationPackage
from shared.schemas import CanonicalUI, ObservationMode, UIElement


PALETTE = [
    "#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff", "#00ffff",
    "#800000", "#008000", "#000080", "#808000", "#800080", "#008080",
    "#ffa500", "#ff1493", "#9932cc", "#20b2aa", "#4b0082", "#00ff7f",
    "#ff6347", "#00ced1", "#9400d3", "#f0e68c", "#ff8c00", "#228b22",
]


def fixture(*, indexed=True, colors=None):
    colors = colors or PALETTE
    samples = colors + ["#ff6347", "#ffff00", "#9932cc"]
    image = Image.new("RGB", (10 * len(samples), 10))
    boxes = [[i * 10, 0, (i + 1) * 10, 10] for i in range(len(samples))]
    for color, box in zip(samples, boxes):
        image.paste(color, box)
    out = BytesIO()
    image.save(out, format="PNG")
    package = ObservationPackage(
        ui=CanonicalUI(elements=[
            UIElement(index=i + 1, bounds=box, clickable=True)
            for i, box in enumerate(boxes[:len(colors)])
        ] if indexed else []),
        mode=ObservationMode.TREE_PLUS_IMAGE if indexed else ObservationMode.IMAGE_ONLY,
        text_for_llm="", gap_reasons=[], clean_png=out.getvalue(),
        image_for_llm=b"unused", annotated_png=None, observation_id="current",
        frame_width=image.width, frame_height=10,
        model_image_width=image.width, model_image_height=10, index_actionable=indexed,
    )
    ctx = ToolExecutionContext(AgentRole.EXECUTOR, "comparison", {
        "active_observation_id": "current", "active_package": package,
    })
    args = {"observation_id": "current"}
    if indexed:
        args.update(targets=[{"index": i + 1} for i in range(len(colors))],
                    regions=[{"bounds": box} for box in boxes[len(colors):]],
                    compare={"references": "all_regions", "candidates": "all_targets"})
    else:
        args.update(regions=[{"bounds": box} for box in boxes], compare={
            "references": [f"region:{i + 1}" for i in range(len(colors), len(samples))],
            "candidates": [f"region:{i + 1}" for i in range(len(colors))],
        })
    return args, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("indexed", [True, False])
async def test_whole_palette_finds_previously_omitted_purple_with_compact_output(indexed):
    args, ctx = fixture(indexed=indexed)
    result = await make_inspect_image_regions_handler()(args, ctx)
    rankings = list(result.data["comparison"]["rankings"].values())
    prefix = "index" if indexed else "region"
    assert [r["top"][0] for r in rankings] == [
        [f"{prefix}:19", 0.0], [f"{prefix}:4", 0.0], [f"{prefix}:15", 0.0],
    ]
    assert all(r["compared_count"] == 24 and len(r["top"]) == 3 for r in rankings)
    assert "regions" not in result.data
    assert sum(len(rows) for rows in result.metadata()["diagnostics"]["delta_e_2000"].values()) == 72
    assert len(result.metadata()["diagnostics"]["regions"]) == 27
    assert "diagnostics" not in result.model_metadata("inspect_image_regions")
    assert sum(len(r["top"]) for r in rankings) == 9
    assert ctx.state["image_region_inspection_calls"] == 1


@pytest.mark.asyncio
async def test_sampling_pairs_and_comparison_can_be_combined_and_fingerprinted():
    args, ctx = fixture(colors=PALETTE[:3])
    handler = make_inspect_image_regions_handler()
    await handler(args, ctx)
    with pytest.raises(ValueError, match="duplicate"):
        await handler(args, ctx)
    args.update(metrics=["median_rgb"], pairs=[["region:1", "index:1"]])
    result = await handler(args, ctx)
    assert result.data["regions"]["index:1"]["median_rgb"] == [255, 0, 0]
    assert len(result.data["delta_e_2000"]) == 1
    assert "comparison" in result.data
    assert result.data["remaining_calls"] == 0


@pytest.mark.asyncio
async def test_ties_are_stable_and_truncation_is_explicit():
    args, ctx = fixture(colors=["#9932cc"] * 4)
    args["compare"]["candidates"] = ["index:4", "index:2", "index:3", "index:1"]
    result = await make_inspect_image_regions_handler()(args, ctx)
    row = result.data["comparison"]["rankings"]["region:3"]
    assert row == {"top": [["index:4", 0.0], ["index:2", 0.0], ["index:3", 0.0]],
                   "compared_count": 4, "ties_truncated": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("change,error", [
    ({"top_k": True}, "top_k"), ({"top_k": 0}, "top_k"),
    ({"top_k": 6}, "top_k"), ({"top_k": 1.5}, "top_k"),
    ({"references": []}, "references"), ({"candidates": ["index:999"]}, "candidates"),
    ({"candidates": ["index:1", "index:1"]}, "candidates"),
    ({"candidates": "all_regions"}, "disjoint"),
    ({"references": "all_targets"}, "references"),
])
async def test_invalid_group_requests_do_not_consume_allowance(change, error):
    args, ctx = fixture()
    args["compare"].update(change)
    with pytest.raises(ValueError, match=error):
        await make_inspect_image_regions_handler()(args, ctx)
    assert not ctx.state.get("image_region_inspection_calls")


@pytest.mark.asyncio
async def test_comparison_does_not_relax_observation_or_sampling_limits():
    args, ctx = fixture()
    handler = make_inspect_image_regions_handler()
    with pytest.raises(ValueError, match="stale"):
        await handler({**args, "observation_id": "old"}, ctx)
    args.pop("compare")
    args["metrics"] = ["median_rgb"]
    with pytest.raises(ValueError, match="1..12"):
        await handler(args, ctx)
    args, ctx = fixture(colors=["red"] * 30)
    with pytest.raises(ValueError, match="1..32"):
        await handler(args, ctx)


@pytest.mark.asyncio
async def test_mixed_regions_remain_visible_in_compact_mode():
    args, ctx = fixture(colors=["red", "blue"])
    args["regions"][0]["bounds"] = [0, 0, 20, 10]
    result = await make_inspect_image_regions_handler()(args, ctx)
    assert "region:1" in result.model_metadata("inspect_image_regions")["data"]["mixed_regions"]
