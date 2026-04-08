"""Tests for the devicetree keymap parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from zmk_studio_push.parser import (
    ParseError,
    ParsedBinding,
    parse_keymap,
    parse_keymap_file,
)


MINIMAL_KEYMAP = """\
#define DEF 0
#define SYM 1

/ {
    keymap {
        compatible = "zmk,keymap";
        layer_Base {
            display-name = "Base";
            bindings = <
                &kp A   &kp B   &kp C
                &trans  &mo SYM &kp RET
            >;
        };
        layer_Sym {
            display-name = "Sym";
            bindings = <
                &trans  &kp N1  &kp N2
                &trans  &trans  &kp N3
            >;
        };
    };
};
"""


class TestMinimalKeymap:
    def test_parses_two_layers(self):
        km = parse_keymap(MINIMAL_KEYMAP, expected_key_count=6)
        assert len(km.layers) == 2
        assert km.layers[0].index == 0
        assert km.layers[0].name == "Base"
        assert km.layers[1].index == 1
        assert km.layers[1].name == "Sym"

    def test_base_layer_bindings(self):
        km = parse_keymap(MINIMAL_KEYMAP, expected_key_count=6)
        base = km.layers[0].bindings
        assert base[0] == ParsedBinding("kp", 0x00070004, 0)   # A
        assert base[1] == ParsedBinding("kp", 0x00070005, 0)   # B
        assert base[2] == ParsedBinding("kp", 0x00070006, 0)   # C
        assert base[3] == ParsedBinding("trans", 0, 0)
        assert base[4] == ParsedBinding("mo", 1, 0)             # SYM layer = 1
        assert base[5] == ParsedBinding("kp", 0x00070028, 0)   # RET

    def test_sym_layer_bindings(self):
        km = parse_keymap(MINIMAL_KEYMAP, expected_key_count=6)
        sym = km.layers[1].bindings
        assert sym[1] == ParsedBinding("kp", 0x0007001E, 0)    # N1
        assert sym[2] == ParsedBinding("kp", 0x0007001F, 0)    # N2
        assert sym[5] == ParsedBinding("kp", 0x00070020, 0)    # N3


class TestBehaviorVariants:
    def _parse_single_layer(self, bindings: str, expected_keys: int = 1) -> list[ParsedBinding]:
        source = f"""
        #define DEF 0
        #define SYM 1
        / {{ keymap {{
            compatible = "zmk,keymap";
            layer_Base {{
                bindings = < {bindings} >;
            }};
        }}; }};
        """
        km = parse_keymap(source, expected_key_count=expected_keys)
        return km.layers[0].bindings

    def test_kp(self):
        assert self._parse_single_layer("&kp F5")[0] == ParsedBinding("kp", 0x0007003E, 0)

    def test_mt(self):
        b = self._parse_single_layer("&mt LCTRL BSPC")[0]
        assert b.behavior_name == "mt"
        # LCTRL = left control keycode 0x000700E0
        assert b.param1 == 0x000700E0
        # BSPC = backspace keycode 0x0007002A
        assert b.param2 == 0x0007002A

    def test_lt(self):
        b = self._parse_single_layer("&lt SYM TAB")[0]
        assert b.behavior_name == "lt"
        assert b.param1 == 1  # SYM layer
        assert b.param2 == 0x0007002B  # TAB

    def test_mo(self):
        assert self._parse_single_layer("&mo SYM")[0] == ParsedBinding("mo", 1, 0)

    def test_to(self):
        assert self._parse_single_layer("&to DEF")[0] == ParsedBinding("to", 0, 0)

    def test_trans(self):
        assert self._parse_single_layer("&trans")[0] == ParsedBinding("trans", 0, 0)

    def test_none(self):
        assert self._parse_single_layer("&none")[0] == ParsedBinding("none", 0, 0)

    def test_modifier_wrapped_keycode(self):
        # LG(N1) = (0x08 << 24) | N1 = 0x0807001E
        b = self._parse_single_layer("&kp LG(N1)")[0]
        assert b == ParsedBinding("kp", 0x0807001E, 0)

    def test_nested_modifier_wrappers(self):
        # LG(LS(A)) = (0x08 << 24) | (0x02 << 24) | A = 0x0A070004
        b = self._parse_single_layer("&kp LG(LS(A))")[0]
        assert b == ParsedBinding("kp", 0x0A070004, 0)


class TestPreprocessor:
    def test_simple_integer_define(self):
        src = """
        #define FUN 4
        / { keymap { compatible = "zmk,keymap";
            layer_Base { bindings = < &mo FUN >; };
        }; };
        """
        km = parse_keymap(src, expected_key_count=1)
        assert km.layers[0].bindings[0] == ParsedBinding("mo", 4, 0)

    def test_multi_token_define_expansion(self):
        src = """
        #define NUM 3
        #define _SMART_NUM &smart_num NUM 0
        / { keymap { compatible = "zmk,keymap";
            layer_Base { bindings = < _SMART_NUM >; };
        }; };
        """
        km = parse_keymap(
            src,
            expected_key_count=1,
            extra_behavior_params={"smart_num": 2},
        )
        assert km.layers[0].bindings[0] == ParsedBinding("smart_num", 3, 0)

    def test_strips_line_comments(self):
        src = """
        #define DEF 0
        // this is a comment
        / { keymap { compatible = "zmk,keymap"; // more comments
            layer_Base { bindings = < &kp A /* inline */ &kp B >; };
        }; };
        """
        km = parse_keymap(src, expected_key_count=2)
        assert km.layers[0].bindings[0].behavior_name == "kp"
        assert km.layers[0].bindings[1].behavior_name == "kp"

    def test_skips_behavior_config_blocks(self):
        src = """
        #define DEF 0
        &sk { release-after-ms = <900>; };
        &lt { tapping-term-ms = <200>; };
        / { keymap { compatible = "zmk,keymap";
            layer_Base { bindings = < &trans >; };
        }; };
        """
        km = parse_keymap(src, expected_key_count=1)
        assert km.layers[0].bindings[0] == ParsedBinding("trans", 0, 0)


class TestValidation:
    def test_unknown_behavior_raises(self):
        src = """
        #define DEF 0
        / { keymap { compatible = "zmk,keymap";
            layer_Base { bindings = < &totally_unknown >; };
        }; };
        """
        with pytest.raises(ParseError, match="unknown behavior"):
            parse_keymap(src, expected_key_count=1)

    def test_unknown_keycode_raises(self):
        src = """
        #define DEF 0
        / { keymap { compatible = "zmk,keymap";
            layer_Base { bindings = < &kp NOT_A_KEY >; };
        }; };
        """
        with pytest.raises(ParseError, match="unknown keycode|resolve"):
            parse_keymap(src, expected_key_count=1)

    def test_wrong_key_count_raises(self):
        src = """
        #define DEF 0
        / { keymap { compatible = "zmk,keymap";
            layer_Base { bindings = < &kp A &kp B >; };
        }; };
        """
        with pytest.raises(ParseError, match="key count"):
            parse_keymap(src, expected_key_count=5)


class TestRealToucanKeymap:
    """End-to-end test parsing the actual config/toucan.keymap."""

    @pytest.fixture
    def toucan_keymap_path(self) -> Path:
        return Path(__file__).parents[3] / "config" / "toucan.keymap"

    def test_parses_all_five_layers(self, toucan_keymap_path: Path):
        km = parse_keymap_file(toucan_keymap_path, expected_key_count=42)
        assert len(km.layers) == 5
        assert [layer.name for layer in km.layers] == [
            "Base",
            "Sym",
            "Nav",
            "Num",
            "Fun",
        ]

    def test_each_layer_has_42_bindings(self, toucan_keymap_path: Path):
        km = parse_keymap_file(toucan_keymap_path, expected_key_count=42)
        for layer in km.layers:
            assert len(layer.bindings) == 42, (
                f"Layer {layer.name} has {len(layer.bindings)} bindings, expected 42"
            )

    def test_base_layer_sample_bindings(self, toucan_keymap_path: Path):
        km = parse_keymap_file(toucan_keymap_path, expected_key_count=42)
        base = km.layers[0].bindings
        # Position 0: &mo FUN → layer 4
        assert base[0] == ParsedBinding("mo", 4, 0)
        # Position 1: &kp Q → 0x00070014
        assert base[1] == ParsedBinding("kp", 0x00070014, 0)
        # Position 12 (ESC on home row) — it's actually at index 12 after the
        # 12 keys in the top row
        assert base[12].behavior_name == "kp"

    def test_fun_layer_has_media_keys(self, toucan_keymap_path: Path):
        km = parse_keymap_file(toucan_keymap_path, expected_key_count=42)
        fun = km.layers[4]
        # Find C_MUTE, C_VOL_DN, C_VOL_UP somewhere
        mute_code = 0x000C00E2
        voldn_code = 0x000C00EA
        volup_code = 0x000C00E9
        values = [(b.behavior_name, b.param1) for b in fun.bindings]
        assert ("kp", mute_code) in values
        assert ("kp", voldn_code) in values
        assert ("kp", volup_code) in values

    def test_fun_layer_has_function_keys(self, toucan_keymap_path: Path):
        km = parse_keymap_file(toucan_keymap_path, expected_key_count=42)
        fun = km.layers[4]
        values = [(b.behavior_name, b.param1) for b in fun.bindings]
        # F1 through F12
        for f_num, offset in zip(range(1, 11), range(0x3A, 0x44)):
            code = (0x07 << 16) | offset
            assert ("kp", code) in values, f"F{f_num} (0x{code:08X}) not found"
