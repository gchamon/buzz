"""Tests for maint-scripts/render_ui_props.py background rendering."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "maint-scripts"
    / "render_ui_props.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("render_ui_props", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


rup = _load_module()


def _center_at_angle(angle_deg: float, global_delta: float = 0.0) -> tuple[float, float]:
    """Resolve a single center at ``angle_deg`` to its normalized (x, y)."""
    props = {
        "background": {
            "polar_delta": global_delta,
            "centers": [
                {"color": "#000000", "polar_delta": angle_deg, "sigma_x": 0.5, "sigma_y": 0.5}
            ],
        }
    }
    cx, cy, *_ = rup._resolve_centers(props, ["#000000"], (1, 1))[0]
    return cx, cy


class BorderPointTests:
    def test_ray_lands_on_border(self):
        for angle in (0, 45, 90, 135, 180, 225, 270, 315, 71.5616):
            x, y = _center_at_angle(angle)
            on_border = (
                abs(x) < 1e-9
                or abs(x - 1.0) < 1e-9
                or abs(y) < 1e-9
                or abs(y - 1.0) < 1e-9
            )
            assert on_border, f"angle {angle}: ({x}, {y}) not on border"

    def test_cardinal_angles(self):
        # math convention with image Y growing downward
        def approx(point, expected):
            return all(abs(a - b) < 1e-9 for a, b in zip(point, expected, strict=True))

        assert approx(_center_at_angle(0), (1.0, 0.5))    # +x -> right edge
        assert approx(_center_at_angle(90), (0.5, 1.0))   # +y -> bottom edge
        assert approx(_center_at_angle(180), (0.0, 0.5))  # -x -> left edge
        assert approx(_center_at_angle(270), (0.5, 0.0))  # -y -> top edge

    def test_corner_angles_match_legacy_positions(self):
        # 225 deg -> top-left corner (0,0); 315 deg -> top-right corner (1,0)
        x, y = _center_at_angle(225)
        assert abs(x) < 1e-9 and abs(y) < 1e-9
        x, y = _center_at_angle(315)
        assert abs(x - 1.0) < 1e-9 and abs(y) < 1e-9


class GlobalDeltaTests:
    def _approx(self, point, expected):
        return all(abs(a - b) < 1e-9 for a, b in zip(point, expected, strict=True))

    def test_zero_offset_is_identity(self):
        assert self._approx(_center_at_angle(0, 0.0), _center_at_angle(0))

    def test_offset_adds_to_center_angle(self):
        # A center at 0 deg with a +90 global offset must land where a plain
        # 90 deg center lands.
        assert self._approx(_center_at_angle(0, 90.0), _center_at_angle(90))
        assert self._approx(_center_at_angle(45, 90.0), _center_at_angle(135))

    def test_rotation_quarter_turns(self):
        # 0 deg center rotated by each quarter turn walks around the border.
        assert self._approx(_center_at_angle(0, 0.0), (1.0, 0.5))    # right
        assert self._approx(_center_at_angle(0, 90.0), (0.5, 1.0))   # bottom
        assert self._approx(_center_at_angle(0, 180.0), (0.0, 0.5))  # left
        assert self._approx(_center_at_angle(0, 270.0), (0.5, 0.0))  # top

    def test_offset_stays_on_border(self):
        for angle in (0, 71.5616, 225):
            for offset in (37.0, 90.0, 180.0, 270.0):
                x, y = _center_at_angle(angle, offset)
                on_border = (
                    abs(x) < 1e-9
                    or abs(x - 1.0) < 1e-9
                    or abs(y) < 1e-9
                    or abs(y - 1.0) < 1e-9
                )
                assert on_border, f"angle {angle}+{offset}: ({x}, {y}) off border"


def _render_digest(centers: list[dict], global_delta: float = 0.0) -> str:
    """Render a small deterministic background and return a pixel-level digest."""
    props = {
        "background": {
            "polar_delta": global_delta,
            "blur_radius": 0.0,
            "centers": centers,
        }
    }
    palette = [c["color"] for c in centers]
    image = rup._render_background(props, palette, (40, 25))
    return hashlib.sha256(image.tobytes()).hexdigest()


def _sample_centers() -> list[dict]:
    # Distinct colors and angles so rotation produces a visibly different render.
    return [
        {"color": "#210c24", "polar_delta": 200.0, "sigma_x": 0.5, "sigma_y": 0.5},
        {"color": "#1f0d27", "polar_delta": 340.0, "sigma_x": 0.5, "sigma_y": 0.5},
        {"color": "#32c6a4", "polar_delta": 80.0, "sigma_x": 0.4, "sigma_y": 0.4},
    ]


class PolarDeltaRenderTests:
    def test_render_completes(self):
        image = rup._render_background(
            {"background": {"polar_delta": 90.0, "centers": _sample_centers()}},
            [c["color"] for c in _sample_centers()],
            (8, 5),
        )
        assert image.size == (8, 5)

    def test_render_is_deterministic(self):
        centers = _sample_centers()
        assert _render_digest(centers, 37.0) == _render_digest(centers, 37.0)

    def test_global_rotation_changes_render(self):
        centers = _sample_centers()
        assert _render_digest(centers, 0.0) != _render_digest(centers, 90.0)
        assert _render_digest(centers, 90.0) != _render_digest(centers, 180.0)

    def test_full_turn_is_identity(self):
        # A 360 deg global rotation must reproduce the un-rotated render.
        centers = _sample_centers()
        assert _render_digest(centers, 0.0) == _render_digest(centers, 360.0)

    def test_distinct_center_angle_changes_render(self):
        base = _sample_centers()
        rotated = [dict(c) for c in base]
        rotated[0]["polar_delta"] = 160.0
        assert _render_digest(base) != _render_digest(rotated)


def _frame_style(border_size: int, rounding: int) -> dict:
    style = rup._hyprland_fallback_style({})
    style["border_size"] = border_size
    style["rounding"] = rounding
    style["active_border_start"] = "#00ff00"
    style["active_border_end"] = "#00cc00"
    style["shadow"] = "#102030"
    return style


def _build_html(props: dict, raw_size=(80, 60), background_size=(200, 170), **style_kw):
    border_size = style_kw.pop("border_size", 2)
    rounding = style_kw.pop("rounding", 8)
    style = _frame_style(border_size, rounding)
    style.update(style_kw)
    return rup._compose_prop_html(
        props,
        "data:image/png;base64,SCREENSHOT",
        raw_size,
        "data:image/png;base64,BACKGROUND",
        background_size,
        style,
        "cache",
    )


class ComposePropHtmlTests:
    def test_auto_viewport_matches_outer_frame_margin(self):
        viewport = rup._viewport(
            {
                "background": {"size": {"width": 1600, "height": 1000}},
                "frame": {"max_width_margin": 20, "max_height_margin": 20},
                "viewport": {"width": "auto", "height": "auto"},
            },
            _frame_style(2, 8),
        )
        assert viewport == {"width": 1556, "height": 908}

    def test_auto_viewport_uses_configured_browser_top_bar_height(self):
        viewport = rup._viewport(
            {
                "background": {"size": {"width": 1600, "height": 1000}},
                "frame": {
                    "max_width_margin": 20,
                    "max_height_margin": 20,
                    "browser": {"top_bar_height": 60},
                },
                "viewport": {"width": "auto", "height": "auto"},
            },
            _frame_style(2, 8),
        )
        assert viewport == {"width": 1556, "height": 896}

    def test_radii_map_from_style(self):
        html = _build_html({}, border_size=3, rounding=8)
        assert "padding:3px" in html
        assert "border-radius:11px" in html  # rounding + border_size on .frame
        assert "border-radius:8px" in html  # rounding on the window contents

    def test_gradient_order_active(self):
        html = _build_html({"frame": {"border_gradient": "active"}})
        # active: start -> end
        assert "linear-gradient(135deg,#00ff00,#00cc00)" in html

    def test_gradient_order_inverted(self):
        html = _build_html({"frame": {"border_gradient": "active_inverted"}})
        # active_inverted: end -> start
        assert "linear-gradient(135deg,#00cc00,#00ff00)" in html

    def test_shadow_rgba_from_color_and_alpha(self):
        html = _build_html(
            {"frame": {"shadow_alpha": 128, "shadow_blur_radius": 14, "shadow_offset_y": 8}}
        )
        # #102030 -> 16,32,48 ; alpha 128/255 = 0.5020
        assert "box-shadow:0 8px 14.0px rgba(16,32,48,0.5020)" in html

    def test_data_uris_embedded(self):
        html = _build_html({})
        assert "url('data:image/png;base64,BACKGROUND')" in html
        assert "src='data:image/png;base64,SCREENSHOT'" in html

    def test_route_is_rendered_in_address_bar(self):
        html = _build_html({})
        assert "groundstation.lan:9443/cache" in html

    def test_browser_chrome_values_are_configurable(self):
        html = _build_html(
            {
                "frame": {
                    "browser": {
                        "address_origin": "demo.example:8443",
                        "top_bar_height": 60,
                        "top_bar_background": "#222222",
                        "address_width": 480,
                        "icon_size": 16,
                    }
                }
            }
        )
        assert "demo.example:8443/cache" in html
        assert "height:60px" in html
        assert "background-color:#222222" in html
        assert "width:480px" in html
        assert ".icon{width:16px;height:16px;}" in html

    def test_screenshot_scaled_to_fit(self):
        # 40px outer margins on 200x170 produce a 120x90 frame.
        # After 2px border padding and 48px top bar, screenshot max is 116x38.
        html = _build_html(
            {"frame": {"max_width_margin": 40, "max_height_margin": 40}},
            raw_size=(400, 300),
        )
        assert "width:50px" in html
        assert "height:38px" in html

    def test_screenshot_not_upscaled(self):
        # raw 40x30 fits comfortably; must not be enlarged.
        html = _build_html(
            {"frame": {"max_width_margin": 40, "max_height_margin": 40}},
            raw_size=(40, 30),
        )
        assert "width:40px" in html
        assert "height:30px" in html


class UiStateConfigTests:
    def test_route_global_state_overrides_shared_global_state(self):
        props = {
            "ui_views": {
                "global": {"curator_log_level": "info", "last_sync_at": "base"},
                "logs": {
                    "global": {
                        "curator_log_level": "error",
                        "nav_log_level": "error",
                    }
                },
            }
        }
        assert rup._route_global_ui_state(props, "logs") == {
            "curator_log_level": "error",
            "last_sync_at": "base",
            "nav_log_level": "error",
        }

    def test_cache_names_and_states_are_configurable(self):
        entries = rup._cache_entries(
            {
                "ui_views": {
                    "cache": [
                        {
                            "name": "Custom Feature",
                            "original_name": "Custom.Feature.1080p",
                            "status": "downloading",
                            "progress": 42,
                            "files": [
                                {
                                    "id": "1",
                                    "path": "/Custom.Feature/feature.mkv",
                                    "selected": False,
                                }
                            ],
                        }
                    ]
                }
            }
        )
        info = entries["rd-feature-001"]["info"]
        assert info["filename"] == "Custom Feature"
        assert info["original_filename"] == "Custom.Feature.1080p"
        assert info["status"] == "downloading"
        assert info["progress"] == 42
        assert info["files"][0]["selected"] is False

    def test_cache_file_selection_clears_alert_state(self):
        entries = rup._cache_entries(
            {
                "ui_views": {
                    "cache": {
                        "entries": [
                            {},
                            {
                                "files": [
                                    {
                                        "id": "1",
                                        "path": "/His Girl Friday/His.Girl.Friday.1940.mkv",
                                        "selected": True,
                                    }
                                ]
                            },
                        ]
                    }
                }
            }
        )
        info = entries["torbox:anime-042"]["info"]
        assert info["files"][0]["selected"] is True

    def test_archive_names_are_configurable(self):
        entries = rup._archive_entries(
            {"ui_views": {"archive": [{"name": "Custom Archive"}]}}
        )
        assert entries["arch1111"]["name"] == "Custom Archive"

    def test_selected_thread_defaults_to_third_configured_thread(self):
        props = {
            "ui_views": {
                "threads": [
                    {"id": "first"},
                    {"id": "second"},
                    {"id": "custom-selected"},
                ]
            }
        }
        assert rup._selected_thread_id(props) == "custom-selected"

    def test_selected_thread_can_be_explicit(self):
        props = {"ui_views": {"threads": {"selected_task_id": "explicit-task"}}}
        assert rup._selected_thread_id(props) == "explicit-task"
