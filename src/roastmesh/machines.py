"""Roaster machine catalogue: a search facet and the Settings machine picker.

Three blocks, kept clearly separate.

1. `ARTISAN_MACHINES` -- derived from Artisan's own machine-setup files
   (`src/includes/Machines/<Manufacturer>/<Model>.aset`, one
   `roastertype_setup=<string>` per file). That string is exactly what
   Artisan writes into a profile's `roastertype` field.

   It is NOT, however, a model name. An audit of all 273 strings found that
   only half name a machine at all: the rest name the control electronics
   ("San Franciscan Eurotherm" is a PID brand), the wiring ("MCR Phidget &
   Delta controls (port on the right)"), the cable ("Kaleido Serial"), or
   nothing that ships in a roaster ("Phidget 2xRTD" is a thermocouple
   board). So `display_name` here is the machine a human would recognise,
   and `artisan_strings` holds the literal string(s) Artisan writes for it.
   Several strings routinely collapse onto one machine -- five separate
   entries described one Coffed SR5, differing only in whether its fans
   were EBM-Papst or Honeywell.

   Where a brand's every string named electronics and nothing else, it gets
   one honest "(model unspecified)" entry rather than a fake model name.
   Its real models live in `RESEARCHED_MACHINES` for the picker to offer.

2. `RESEARCHED_MACHINES` -- real model names for brands Artisan either
   doesn't cover or covers only by controller. These carry no
   `artisan_strings`: no .alog will ever contain them, because Artisan has
   no such string to write. They exist so a user can pick their actual
   machine in Settings. Sourced from manufacturer sites and dealer
   listings; only HIGH-confidence findings were taken.

3. `HOME_ROASTER_SUPPLEMENT` -- hand-written home roasters Artisan's
   catalogue omits entirely (it only covers connected/commercial machines).
   Append freely; there is no generation step to rerun for it.

Nothing here talks to the network or expects an Artisan checkout at runtime.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Machine:
    key: str             # matches roasts.machine_key's vocabulary
    display_name: str    # the model a human would recognise on their roaster
    manufacturer: str
    # The literal roastertype string(s) Artisan writes for this machine.
    # Empty means "Artisan has no string for this" -- a picker-only entry,
    # which then matches its own display_name if a file ever carries it.
    artisan_strings: tuple[str, ...] = ()
    mechanism_family: str = "unknown"

    @property
    def match_strings(self) -> tuple[str, ...]:
        return self.artisan_strings or (self.display_name,)


# Artisan ships machine-setup files for hardware that is not a roaster:
# three Phidget thermocouple interface boards and one Artisan plugin. They
# are deliberately absent from the catalogue above, but that alone is not
# enough -- an unrecognised string falls through to slugify, so dropping
# them from the picker would just move the problem, giving a file that
# names an interface board its own machine facet. Named here (lowercased)
# so ingest can route them to "unknown" instead.
NOT_A_ROASTER: frozenset[str] = frozenset({
    "phidget 2xrtd", "phidget 2xtc", "phidget databridge", "plugin roast",
})


def slugify(text: str) -> str:
    """Same slugification alog/machine.py's own fallback uses, so catalogue
    keys stay consistent with what an unrecognized roastertype string has
    always produced.

    Known wart, kept deliberately: "+" is not encoded, so "Giesen WxA" and
    "Giesen WxA+" both slug to `giesen_wxa` and merge in the facet despite
    being different machines (the same reason "Hottop 2K+" is `hottop_2k`).
    Fixing it would re-key entries that have already shipped, so it is
    recorded here rather than changed as a side effect of this pass.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "unknown"


# Brands whose machine_key and mechanism_family predate this catalogue and
# own them outright. Slugifying a display_name is the right key for the
# machines nothing else claims, but for these brands it would invent a
# *second* vocabulary for a machine that already has one: the catalogue
# would advertise "aillio_bullet_r1" in a picker while every Bullet roast
# ever ingested is stored under "aillio_bullet". A user who picked their own
# machine in Settings would then match none of their own roasts, which is
# precisely what the machine facet exists to do. Caught end-to-end, not by a
# unit test -- `profile set --machine aillio_bullet` was rejected as unknown
# while being the only key the index actually contains.
#
# The precise model is not lost: it stays in display_name, which is what
# users.machine_display stores and what a picker shows. Several rows
# therefore share one key (four Kaleido M1 editions, two Bullets, two
# Hottops), which is correct -- they are one searchable machine.
#
# This is the ONE implementation of these rules. alog/machine.py imports
# `effective_key`/`effective_family` from here rather than restating them,
# so the two cannot drift.
_ALIAS_RULES: tuple[tuple[str, str, str], ...] = (
    ("hottop", "hottop", "hottop_drum"),
    ("behmor", "behmor", "behmor_drum"),
    ("bullet", "aillio_bullet", "aillio_fluidbed"),
)
# Model token in a Kaleido name: M1/M2/M6/M10 are capacities, K3 a separate
# line. Artisan's own three Kaleido strings carry none of them -- see
# `Kaleido (model unspecified)` in ARTISAN_MACHINES.
_KALEIDO_MODEL_RE = re.compile(r"\b([km])(\d+)\b")


def effective_key(display_name: str) -> str:
    """The machine_key an .alog carrying this exact string would be stored
    under -- i.e. the catalogue's key must be what ingest produces, never a
    parallel vocabulary."""
    text = display_name.strip().lower()
    if "kaleido" in text:
        model = _KALEIDO_MODEL_RE.search(text)
        if model:
            return f"kaleido_{model.group(1)}{model.group(2)}"
        return "kaleido_legacy" if "legacy" in text else "kaleido_serial"
    for substring, key, _family in _ALIAS_RULES:
        if substring in text:
            return key
    return slugify(display_name)


def effective_family(display_name: str) -> str:
    """Mechanism family for the brands this project has trustworthy data
    for. "unknown" everywhere else: the catalogue has no reliable
    drum/fluidbed data for the ~250 machines Artisan merely lists, and
    inventing it would poison an existing search facet."""
    text = display_name.strip().lower()
    if "kaleido" in text:
        return "kaleido_drum"
    for substring, _key, family in _ALIAS_RULES:
        if substring in text:
            return family
    return "unknown"


# ---------------------------------------------------------------------------
# GENERATED -- do not hand-edit. Regenerate with:
#     .venv/bin/python tools/build_machine_catalogue.py <path-to-Machines-dir>
# 273 Artisan strings collapsed onto the machines they actually describe.
# ---------------------------------------------------------------------------
ARTISAN_MACHINES: tuple[Machine, ...] = (
    Machine('aillio_bullet', 'Aillio Bullet R1', 'Aillio', artisan_strings=('Aillio Bullet R1',), mechanism_family='aillio_fluidbed'),
    Machine('aillio_bullet', 'Aillio Bullet R1 IBTS', 'Aillio', artisan_strings=('Aillio Bullet R1 IBTS',), mechanism_family='aillio_fluidbed'),
    Machine('aillio_bullet', 'Aillio Bullet R2', 'Aillio', artisan_strings=('Aillio Bullet R2',), mechanism_family='aillio_fluidbed'),
    Machine('ambex_ym', 'Ambex YM', 'Ambex', artisan_strings=('Ambex YM',)),
    Machine('arc_800', 'Arc 800', 'Arc', artisan_strings=('ARC 800 RTD', 'Arc 800')),
    Machine('arc_s', 'Arc S', 'Arc', artisan_strings=('ARC S RTD', 'Arc S')),
    Machine('atilla_gold', 'Atilla GOLD', 'Atilla', artisan_strings=('Atilla GOLD',)),
    Machine('atilla_gold_ii', 'Atilla GOLD II', 'Atilla', artisan_strings=('Atilla GOLD II',)),
    Machine('atilla_gold_plus_ii', 'Atilla GOLD plus II', 'Atilla', artisan_strings=('Atilla GOLD plus II Control', 'Atilla GOLD plus II Control Auto')),
    Machine('bc_roaster', 'BC Roaster', 'BC', artisan_strings=('BC Roaster',)),
    Machine('beango_cube_x', 'BeanGo Cube X', 'BeanGo Cube', artisan_strings=('BeanGo Cube X',)),
    Machine('bella_tw', 'Bella TW', 'BellaTW', artisan_strings=('Bella TW',)),
    Machine('berto', 'Berto (model unspecified)', 'Berto', artisan_strings=('Berto Autonics Control',)),
    Machine('berto_d', 'Berto D', 'Berto', artisan_strings=('Berto D',)),
    Machine('berto_essential', 'Berto Essential', 'Berto', artisan_strings=('Berto Essential',)),
    Machine('berto_one', 'Berto One', 'Berto', artisan_strings=('Berto One',)),
    Machine('berto_r', 'Berto R', 'Berto', artisan_strings=('Berto R',)),
    Machine('besca_bee', 'Besca Bee', 'Besca', artisan_strings=('Besca Bee', 'Besca Bee v2')),
    Machine('besca_bsc', 'Besca BSC', 'Besca', artisan_strings=('Besca BSC auto', 'Besca BSC full-auto', 'Besca BSC manual v1', 'Besca BSC manual v2')),
    Machine('bideli_roaster', 'Bideli Roaster', 'Bideli', artisan_strings=('Bideli Roaster',)),
    Machine('blueking_bk', 'BlueKing BK', 'BlueKing', artisan_strings=('BlueKing BK',)),
    Machine('b_hler_rm_20', 'Bühler RM 20', 'Bühler', artisan_strings=('Bühler RM 20 Playone', 'Bühler RM 20 Simatic', 'Bühler RM 20 Simatic Legacy')),
    Machine('b_hler_rm_60_240', 'Bühler RM 60-240', 'Bühler', artisan_strings=('Bühler RM 60-240',)),
    Machine('caparao', 'Caparao (model unspecified)', 'Caparao', artisan_strings=('Caparao PLC',)),
    Machine('carmomaq_caloratto', 'Carmomaq Caloratto', 'Carmomaq', artisan_strings=('Carmomaq Caloratto', 'Carmomaq Caloratto/Materattor Legacy')),
    Machine('carmomaq_masteratto', 'Carmomaq Masteratto', 'Carmomaq', artisan_strings=('Carmomaq Masteratto',)),
    Machine('carmomaq_speciatto', 'Carmomaq Speciatto', 'Carmomaq', artisan_strings=('Carmomaq Speciatto',)),
    Machine('carmomaq_stratto', 'Carmomaq Stratto', 'Carmomaq', artisan_strings=('Carmomaq Stratto',)),
    Machine('carmomaq_stratto_lab', 'Carmomaq Stratto Lab', 'Carmomaq', artisan_strings=('Carmomaq Stratto Lab',)),
    Machine('coffed_sr15', 'Coffed SR15', 'Coffed', artisan_strings=('Coffed SR15 automatic', 'Coffed SR15 manual delta')),
    Machine('coffed_sr25', 'Coffed SR25', 'Coffed', artisan_strings=('Coffed SR25',)),
    Machine('coffed_sr3', 'Coffed SR3', 'Coffed', artisan_strings=('Coffed SR3 manual', 'Coffed SR3 manual delta', 'Coffed SR3 manual delta+ EBM-Papst', 'Coffed SR3 manual delta+ Honeywell')),
    Machine('coffed_sr5', 'Coffed SR5', 'Coffed', artisan_strings=('Coffed SR5 automatic', 'Coffed SR5 manual', 'Coffed SR5 manual delta', 'Coffed SR5 manual delta+ EBM-Papst', 'Coffed SR5 manual delta+ Honeywell')),
    Machine('coffed_sr60', 'Coffed SR60', 'Coffed', artisan_strings=('Coffed SR60',)),
    Machine('cms_1', 'CMS-1', 'Coffee Machines Sale', artisan_strings=('CMS-1',)),
    Machine('cms_6_30', 'CMS-6-30', 'Coffee Machines Sale', artisan_strings=('CMS-6-30',)),
    Machine('cte_fz_94', 'CTE FZ-94', 'Coffee-Tech', artisan_strings=('CTE FZ-94',)),
    Machine('cte_fz94_evo', 'CTE FZ94 EVO', 'Coffee-Tech', artisan_strings=('CTE FZ94 EVO',)),
    Machine('cte_ghibli', 'CTE Ghibli', 'Coffee-Tech', artisan_strings=('CTE Ghibli',)),
    Machine('cte_ghibli_touch', 'CTE Ghibli Touch', 'Coffee-Tech', artisan_strings=('CTE Ghibli Touch',)),
    Machine('cte_silon', 'CTE Silon', 'Coffee-Tech', artisan_strings=('CTE Silon Touch', 'CTE Silon USB')),
    Machine('coffeetool', 'Coffeetool', 'Coffeetool', artisan_strings=('Coffeetool',)),
    Machine('cogen_series_c', 'Cogen Series C', 'Cogen', artisan_strings=('Cogen Series C', 'Cogen Series C v2')),
    Machine('craftsmith_craft', 'Craftsmith Craft', 'Craftsmith', artisan_strings=('Craftsmith Craft',)),
    Machine('craftsmith_craft_air', 'Craftsmith Craft air', 'Craftsmith', artisan_strings=('Craftsmith Craft air',)),
    Machine('craftsmith_diy', 'Craftsmith DIY', 'Craftsmith', artisan_strings=('Craftsmith DIY',)),
    Machine('d_tgen_dr', 'Dätgen DR', 'Daetgen', artisan_strings=('Dätgen DR',)),
    Machine('d_tgen_dw', 'Dätgen DW', 'Daetgen', artisan_strings=('Dätgen DW',)),
    Machine('diedrich', 'Diedrich (model unspecified)', 'Diedrich', artisan_strings=('Diedrich 4-Sensor', 'Diedrich 6-Sensor', 'Diedrich 6-Sensor (Pre-2018)', 'Diedrich CR', 'Diedrich DR')),
    Machine('dongyi_br', 'Dongyi BR', 'Dongyi', artisan_strings=('Dongyi BR',)),
    Machine('dongyi_by', 'Dongyi BY', 'Dongyi', artisan_strings=('Dongyi BY',)),
    Machine('dongyi_dy', 'Dongyi DY', 'Dongyi', artisan_strings=('Dongyi DY',)),
    Machine('dmr15_a', 'DMR15-A', 'Dutch Master Roaster', artisan_strings=('DMR15-A',)),
    Machine('dmr5_a', 'DMR5-A', 'Dutch Master Roaster', artisan_strings=('DMR5-A',)),
    Machine('easyster_airpressure', ' Easyster AirPressure', 'Easyster', artisan_strings=(' Easyster AirPressure',)),
    Machine('easyster', 'Easyster', 'Easyster', artisan_strings=('Easyster', 'Easyster 3Temp')),
    Machine('easyster_smart', 'Easyster Smart', 'Easyster', artisan_strings=('Easyster Smart',)),
    Machine('fabrica', 'Fabrica', 'Fabrica', artisan_strings=('Fabrica',)),
    Machine('froco_advanced', 'Froco Advanced', 'Froco', artisan_strings=('Froco Advanced',)),
    Machine('froco_improved', 'Froco Improved', 'Froco', artisan_strings=('Froco Improved',)),
    Machine('garanti_gkpx', 'Garanti GKPX', 'Garanti', artisan_strings=('Garanti GKPX',)),
    Machine('giesen_gpe', 'Giesen GPE', 'Giesen', artisan_strings=('Giesen GPE',)),
    Machine('giesen_w140a_v1', 'Giesen W140A v1', 'Giesen', artisan_strings=('Giesen W140A v1',)),
    Machine('giesen_w15a', 'Giesen W15A', 'Giesen', artisan_strings=('Giesen W15A',)),
    Machine('giesen_w15e', 'Giesen W15E', 'Giesen', artisan_strings=('Giesen W15E',)),
    Machine('giesen_w1a', 'Giesen W1A', 'Giesen', artisan_strings=('Giesen W1A',)),
    Machine('giesen_w1e', 'Giesen W1E', 'Giesen', artisan_strings=('Giesen W1E',)),
    Machine('giesen_w30a', 'Giesen W30A', 'Giesen', artisan_strings=('Giesen W30A',)),
    Machine('giesen_w30a_pro', 'Giesen W30A PRO', 'Giesen', artisan_strings=('Giesen W30A PRO',)),
    Machine('giesen_w45a', 'Giesen W45A', 'Giesen', artisan_strings=('Giesen W45A',)),
    Machine('giesen_w60a', 'Giesen W60A', 'Giesen', artisan_strings=('Giesen W60A',)),
    Machine('giesen_w6a', 'Giesen W6A', 'Giesen', artisan_strings=('Giesen W6A',)),
    Machine('giesen_w6a_pro', 'Giesen W6A PRO', 'Giesen', artisan_strings=('Giesen W6A PRO',)),
    Machine('giesen_w6e', 'Giesen W6E', 'Giesen', artisan_strings=('Giesen W6E',)),
    Machine('giesen_wpg', 'Giesen WPG', 'Giesen', artisan_strings=('Giesen WPG',)),
    Machine('giesen_wxa', 'Giesen WxA', 'Giesen', artisan_strings=('Giesen WxA',)),
    Machine('giesen_wxa_coarse', 'Giesen WxA coarse', 'Giesen', artisan_strings=('Giesen WxA coarse',)),
    Machine('giesen_wxa_ir', 'Giesen WxA IR', 'Giesen', artisan_strings=('Giesen WxA IR',)),
    Machine('giesen_wxa_ir_env', 'Giesen WxA IR Env', 'Giesen', artisan_strings=('Giesen WxA IR Env',)),
    Machine('giesen_wxa', 'Giesen WxA+', 'Giesen', artisan_strings=('Giesen WxA+',)),
    Machine('giesen_wxa_ir', 'Giesen WxA+ IR', 'Giesen', artisan_strings=('Giesen WxA+ IR',)),
    Machine('giesen_wxa_ir_env', 'Giesen WxA+ IR Env', 'Giesen', artisan_strings=('Giesen WxA+ IR Env',)),
    Machine('golden_roasters_gr', 'Golden Roasters GR', 'Golden Roasters', artisan_strings=('GR 2xEMKO', 'GR Automatic', 'GR Delta', 'GR Legacy', 'GR Manual')),
    Machine('has_garanti_hgs', 'Has Garanti HGS', 'Has Garanti', artisan_strings=('Has Garanti HGS',)),
    Machine('has_garanti_hsr', 'Has Garanti HSR', 'Has Garanti', artisan_strings=('Has Garanti HSR',)),
    Machine('hb_model_s', 'HB Model S', 'HB', artisan_strings=('HB Model S',)),
    Machine('hb_standard', 'HB Standard', 'HB', artisan_strings=('HB Standard',)),
    Machine('hive_cascabel', 'Hive Cascabel', 'Hive Roaster', artisan_strings=('Hive Roaster Data Dome',)),
    Machine('hottop', 'Hottop (model unspecified)', 'Hottop', artisan_strings=('Hottop TC4',), mechanism_family='hottop_drum'),
    Machine('hottop', 'Hottop KN-8828B-2K+', 'Hottop', artisan_strings=('Hottop 2K+',), mechanism_family='hottop_drum'),
    Machine('ikawa_home', 'IKAWA HOME', 'IKAWA', artisan_strings=('IKAWA HOME',)),
    Machine('ikawa_pro', 'IKAWA PRO', 'IKAWA', artisan_strings=('IKAWA PRO',)),
    Machine('ikawa_pro_x', 'IKAWA PRO X', 'IKAWA', artisan_strings=('IKAWA PRO X',)),
    Machine('imf_rm', 'IMF RM', 'IMF', artisan_strings=('IMF RM', 'IMF RM Auto', 'IMF RM Control', 'IMF RM legacy')),
    Machine('irm_series_mitsubishi', 'iRm Series Mitsubishi', 'iRm Series', artisan_strings=('iRm Series Mitsubishi',)),
    Machine('irm_series_omron', 'iRm Series Omron', 'iRm Series', artisan_strings=('iRm Series Omron',)),
    Machine('joper', 'Joper (model unspecified)', 'Joper', artisan_strings=('Joper PLC',)),
    Machine('kaldi_fortis', 'Kaldi Fortis', 'Kaldi', artisan_strings=('Kaldi Fortis',)),
    Machine('kaleido_serial', 'Kaleido (model unspecified)', 'Kaleido', artisan_strings=('Kaleido Network', 'Kaleido Serial'), mechanism_family='kaleido_drum'),
    Machine('kaleido_legacy', 'Kaleido Legacy (model unspecified)', 'Kaleido', artisan_strings=('Kaleido Legacy',), mechanism_family='kaleido_drum'),
    Machine('kapok', 'KapoK', 'KapoK', artisan_strings=('KapoK', 'KapoK Inlet')),
    Machine('probat_g_ug', 'Probat G/UG', 'Kirsch+Mausser', artisan_strings=('Probat G/UG', 'Probat G/UG control')),
    Machine('kraffe', 'Kraffe (model unspecified)', 'Kraffe', artisan_strings=('Kraffe PLC',)),
    Machine('kuban_supreme', 'Kuban Supreme', 'Kuban', artisan_strings=('Kuban Supreme Automatic', 'Kuban Supreme Manual')),
    Machine('lilla', 'Lilla', 'Lilla', artisan_strings=('Lilla',)),
    Machine('loring', 'Loring', 'Loring', artisan_strings=('Loring', 'Loring Auto')),
    Machine('mill_city_roasters', 'Mill City Roasters (model unspecified)', 'Mill City Roasters', artisan_strings=('MCR Digital Control Panel 1000', 'MCR Digital Control Panel 1000 C', 'MCR Digital Control Panel 1200 C', 'MCR Phidget', 'MCR Phidget & Delta controls (port on the back)', 'MCR Phidget & Delta controls (port on the right)', 'MCR Phidget & Delta controls (port on the right) C', 'MCR Phidget & Shihlin controls (port on the back)', 'MCR Standard Control Panel (Delta)', 'MCR Standard Control Panel (Delta) C', 'MCR Standard Control Panel (Fotek)', 'MCR Standard Control Panel (Fotek) C')),
    Machine('mugma_1000', 'Mugma 1000', 'Mugma', artisan_strings=('Mugma 1000',)),
    Machine('mugma_2000', 'Mugma 2000', 'Mugma', artisan_strings=('Mugma 2000',)),
    Machine('neuhaus_neotec_neoroast', 'Neuhaus Neotec Neoroast', 'Neuhaus Neotec', artisan_strings=('Neuhaus Neotec Neoroast',)),
    Machine('neuhaus_neotec_rfb', 'Neuhaus Neotec RFB', 'Neuhaus Neotec', artisan_strings=('Neuhaus Neotec RFB',)),
    Machine('nor', 'NOR (model unspecified)', 'NOR', artisan_strings=('NOR Extension MODBUS',)),
    Machine('nor_a_series', 'NOR A Series', 'NOR', artisan_strings=('NOR A Series',)),
    Machine('nor_n_series', 'NOR N Series', 'NOR', artisan_strings=('NOR N Series',)),
    Machine('nordic', 'Nordic (model unspecified)', 'Nordic', artisan_strings=('Nordic Delta DTA', 'Nordic Delta DTK', 'Nordic PLC')),
    Machine('north', 'North (model unspecified)', 'North', artisan_strings=('North Standard Control Panel (Fotek)', 'North Standard Control Panel (Fotek) C')),
    Machine('opp_mr', 'Opp MR', 'Opp', artisan_strings=('Opp MR',)),
    Machine('orbiter_ob_1', 'Orbiter OB-1', 'Orbiter', artisan_strings=('Orbiter OB-1',)),
    Machine('otesla', 'OTesla', 'OTesla', artisan_strings=('OTesla',)),
    Machine('zt_rk_oks', 'Öztürk OKS', 'Ozturk', artisan_strings=('Öztürk OKS',)),
    Machine('petroncini', 'Petroncini (model unspecified)', 'Petroncini', artisan_strings=('Petroncini ASEM',)),
    Machine('petroncini_maestro', 'Petroncini Maestro', 'Petroncini', artisan_strings=('Petroncini Maestro',)),
    Machine('petroncini_maestro_i06', 'Petroncini Maestro i06', 'Petroncini', artisan_strings=('Petroncini Maestro i06',)),
    Machine('petroncini_traditional', 'Petroncini Traditional', 'Petroncini', artisan_strings=('Petroncini Traditional',)),
    Machine('phoenix_oro', 'Phoenix ORO', 'Phoenix', artisan_strings=('Phoenix ORO PXF',)),
    Machine('phoenix_roaster', 'Phoenix Roaster', 'Phoenix', artisan_strings=('Phoenix Roaster',)),
    Machine('pratter', 'Pratter (model unspecified)', 'Pratter', artisan_strings=('Pratter Autonics', 'Pratter PLC')),
    Machine('primo_xr', 'Primo Xr', 'Primo', artisan_strings=('Primo Xr',)),
    Machine('prisma', 'Prisma (model unspecified)', 'Prisma', artisan_strings=('Prisma PLC', 'Prisma USB')),
    Machine('proaster', 'Proaster', 'Proaster', artisan_strings=('Proaster', 'Proaster 3Temp', 'Proaster AirPressure')),
    Machine('proaster_thcr_01a', 'Proaster THCR-01A', 'Proaster', artisan_strings=('Proaster THCR-01A',)),
    Machine('probat_g_ug_websockets', ' Probat G/UG WebSockets', 'Probat', artisan_strings=(' Probat G/UG WebSockets',)),
    Machine('probat_p_series', 'Probat P Series', 'Probat', artisan_strings=('Probat P Series',)),
    Machine('probat_sample', 'Probat Sample', 'Probat', artisan_strings=('Probat Sample',)),
    Machine('probatone', 'Probatone', 'Probat', artisan_strings=('Probatone',)),
    Machine('prometheus_ignis', 'Prometheus Ignis', 'Prometheus', artisan_strings=('Prometheus Ignis',)),
    Machine('r_r_r_rv', 'R&R R/RV', 'R & R', artisan_strings=('R&R R/RV Automatic', 'R&R R/RV Manual')),
    Machine('rasco_mac_rm', 'Rasco Mac RM', 'Rasco Mac', artisan_strings=('Rasco Mac RM',)),
    Machine('roastmax', 'Roastmax', 'Roastmax', artisan_strings=('Roastmax',)),
    Machine('roest_100', 'ROEST 100', 'ROEST', artisan_strings=('ROEST 100',)),
    Machine('roest_200', 'ROEST 200', 'ROEST', artisan_strings=('ROEST 200',)),
    Machine('roest_p3000', 'ROEST P3000', 'ROEST', artisan_strings=('ROEST P3000',)),
    Machine('rolltech_el', 'Rolltech EL', 'Rolltech', artisan_strings=('Rolltech EL',)),
    Machine('san_franciscan', 'San Franciscan', 'San Franciscan', artisan_strings=('San Franciscan', 'San Franciscan Eurotherm')),
    Machine('santoker_1xpxr', 'Santoker 1xPXR', 'Santoker', artisan_strings=('Santoker 1xPXR',)),
    Machine('santoker_2xpxf', 'Santoker 2xPXF', 'Santoker', artisan_strings=('Santoker 2xPXF',)),
    Machine('santoker_2xpxr', 'Santoker 2xPXR', 'Santoker', artisan_strings=('Santoker 2xPXR',)),
    Machine('santoker_cube', 'Santoker Cube', 'Santoker', artisan_strings=('Santoker Cube BT', 'Santoker Cube PID')),
    Machine('santoker_q_x_series', 'Santoker Q + X Series', 'Santoker', artisan_strings=('Santoker Q + X Series BT', 'Santoker Q + X Series WiFi')),
    Machine('santoker_r_master_series', 'Santoker R Master Series', 'Santoker', artisan_strings=('Santoker R Master Series BT', 'Santoker R Master Series WiFi')),
    Machine('santoker_r_series', 'Santoker R Series', 'Santoker', artisan_strings=('Santoker R Series BT', 'Santoker R Series USB')),
    Machine('schuilenburg', 'Schuilenburg (model unspecified)', 'Schuilenburg', artisan_strings=('Schuilenburg PLC',)),
    Machine('sedona_elite', 'Sedona Elite', 'Sedona Elite', artisan_strings=('Sedona Elite', 'Sedona Elite PXF')),
    Machine('sedona_elite_2in1', 'Sedona Elite 2in1', 'Sedona Elite', artisan_strings=('Sedona Elite 2in1',)),
    Machine('susa', 'SUSA', 'SEVVALUSA', artisan_strings=('SUSA',)),
    Machine('sivetz_srm', 'Sivetz SRM', 'Sivetz', artisan_strings=('Sivetz SRM', 'Sivetz SRM legacy')),
    Machine('sweet_coffee_italia_gemma_26_30ind', 'Sweet Coffee Italia – Gemma 26-30IND', 'Sweet Coffee Italia', artisan_strings=('Sweet Coffee Italia – Gemma 26-30IND',)),
    Machine('sweet_coffee_italia_gemma_2ind', 'Sweet Coffee Italia – Gemma 2IND', 'Sweet Coffee Italia', artisan_strings=('Sweet Coffee Italia – Gemma 2IND',)),
    Machine('sweet_coffee_italia_gemma_6_8ind', 'Sweet Coffee Italia – Gemma 6-8IND', 'Sweet Coffee Italia', artisan_strings=('Sweet Coffee Italia – Gemma 6-8IND',)),
    Machine('titanium_tgx', 'Titanium TGX', 'Titanium', artisan_strings=('Titanium TGX',)),
    Machine('toper', 'Toper (model unspecified)', 'Toper', artisan_strings=('Toper USB',)),
    Machine('toper_tkm_sx', 'Toper TKM-SX', 'Toper', artisan_strings=('Toper TKM-SX', 'Toper TKM-SX Control')),
    Machine('tostabar_genius', 'Tostabar Genius', 'Tostabar', artisan_strings=('Tostabar Genius',)),
    Machine('trinitas_t2', 'TRINITAS T2', 'TRINITAS', artisan_strings=('TRINITAS T2', 'TRINITAS T2 legacy')),
    Machine('trinitas_t2_air', 'TRINITAS T2 air', 'TRINITAS', artisan_strings=('TRINITAS T2 air',)),
    Machine('trinitas_t7', 'TRINITAS T7', 'TRINITAS', artisan_strings=('TRINITAS T7', 'TRINITAS T7 legacy')),
    Machine('trinitas_t7_gas', 'TRINITAS T7 gas', 'TRINITAS', artisan_strings=('TRINITAS T7 gas',)),
    Machine('twino_ozstar', 'Twino/Ozstar', 'Twino', artisan_strings=('Twino/Ozstar',)),
    Machine('typhoon_hybrid', 'Typhoon Hybrid', 'Typhoon', artisan_strings=('Typhoon Hybrid',)),
    Machine('typhoon_shoproaster', 'Typhoon Shoproaster', 'Typhoon', artisan_strings=('Typhoon Shoproaster',)),
    Machine('us_roaster_corp', 'US Roaster Corp', 'US Roaster Corp', artisan_strings=('US Roaster Corp',)),
    Machine('vnt', 'VNT (model unspecified)', 'VNT', artisan_strings=('VNT PID', 'VNT Phidget')),
    Machine('vortecs_pro', 'Vortecs Pro', 'Vortecs', artisan_strings=('Vortecs Pro',)),
    Machine('wintop_wb', 'Wintop WB', 'Wintop', artisan_strings=('Wintop WB',)),
    Machine('wintop_wk', 'Wintop WK', 'Wintop', artisan_strings=('Wintop WK',)),
    Machine('wintop_ws', 'Wintop WS', 'Wintop', artisan_strings=('Wintop WS 2in1', 'Wintop WS Fuji')),
    Machine('yangchia_8xxn', 'Yangchia 8xxn', 'Yangchia', artisan_strings=('Yangchia 8xxn',)),
    Machine('yoshan_br', 'Yoshan BR', 'Yoshan', artisan_strings=('Yoshan BR',)),
    Machine('yoshan_by', 'Yoshan BY', 'Yoshan', artisan_strings=('Yoshan BY',)),
    Machine('yoshan_dy', 'Yoshan DY', 'Yoshan', artisan_strings=('Yoshan DY',)),
    Machine('yoshan_x', 'Yoshan X', 'Yoshan', artisan_strings=('Yoshan X',)),
    Machine('yoshan_ys', 'Yoshan YS', 'Yoshan', artisan_strings=('Yoshan YS',)),
)

# ---------------------------------------------------------------------------
# RESEARCHED -- real model names for brands Artisan covers only by
# controller, or not at all. Picker-only: no .alog carries these.
# ---------------------------------------------------------------------------
RESEARCHED_MACHINES: tuple[Machine, ...] = (
    Machine('aillio_bullet', 'Aillio Bullet R1 V2', 'Aillio', mechanism_family='aillio_fluidbed'),
    Machine('aillio_bullet', 'Aillio Bullet R2 Pro', 'Aillio', mechanism_family='aillio_fluidbed'),
    Machine('ambex_ym_10', 'Ambex YM-10', 'Ambex'),
    Machine('ambex_ym_120', 'Ambex YM-120', 'Ambex'),
    Machine('ambex_ym_15', 'Ambex YM-15', 'Ambex'),
    Machine('ambex_ym_2', 'Ambex YM-2', 'Ambex'),
    Machine('ambex_ym_30', 'Ambex YM-30', 'Ambex'),
    Machine('ambex_ym_5', 'Ambex YM-5', 'Ambex'),
    Machine('ambex_ym_60', 'Ambex YM-60', 'Ambex'),
    Machine('ambex_ym_90', 'Ambex YM-90', 'Ambex'),
    Machine('bc_1', 'BC-1', 'BC Roasters'),
    Machine('bc_15', 'BC-15', 'BC Roasters'),
    Machine('bc_2', 'BC-2', 'BC Roasters'),
    Machine('bc_25', 'BC-25', 'BC Roasters'),
    Machine('bc_3_5_hd', 'BC-3.5 HD', 'BC Roasters'),
    Machine('bc_35', 'BC-35', 'BC Roasters'),
    Machine('bc_5', 'BC-5', 'BC Roasters'),
    Machine('bc_8', 'BC-8', 'BC Roasters'),
    Machine('behmor', 'Behmor 2000AB Plus', 'Behmor', mechanism_family='behmor_drum'),
    Machine('berto_essential_air', 'Berto Essential Air', 'Berto'),
    Machine('berto_type_d_dl', 'Berto Type D — DL', 'Berto'),
    Machine('berto_type_d_dm', 'Berto Type D — DM', 'Berto'),
    Machine('berto_type_d_ds', 'Berto Type D — DS', 'Berto'),
    Machine('berto_type_r_rl', 'Berto Type R — RL', 'Berto'),
    Machine('berto_type_r_rm', 'Berto Type R — RM', 'Berto'),
    Machine('berto_type_r_rs', 'Berto Type R — RS', 'Berto'),
    Machine('coffee_crafters_duo', 'Coffee Crafters Duo', 'Coffee Crafters'),
    Machine('coffee_crafters_valenta_12', 'Coffee Crafters Valenta 12', 'Coffee Crafters'),
    Machine('coffee_crafters_valenta_3', 'Coffee Crafters Valenta 3', 'Coffee Crafters'),
    Machine('coffeetool_r3', 'Coffeetool R3', 'Coffeetool'),
    Machine('coffeetool_r5', 'Coffeetool R5', 'Coffeetool'),
    Machine('coffeetool_r500', 'Coffeetool R500', 'Coffeetool'),
    Machine('cogen_c15', 'Cogen C15', 'Cogen'),
    Machine('cogen_c2', 'Cogen C2', 'Cogen'),
    Machine('cogen_c30', 'Cogen C30', 'Cogen'),
    Machine('cogen_c6', 'Cogen C6', 'Cogen'),
    Machine('diedrich_dr_25', 'Diedrich DR-25', 'Diedrich'),
    Machine('diedrich_dr_3_dr3_e', 'Diedrich DR-3 / DR3-E', 'Diedrich'),
    Machine('diedrich_dr_35', 'Diedrich DR-35', 'Diedrich'),
    Machine('diedrich_ir_1', 'Diedrich IR-1', 'Diedrich'),
    Machine('diedrich_ir_12', 'Diedrich IR-12', 'Diedrich'),
    Machine('diedrich_ir_5', 'Diedrich IR-5', 'Diedrich'),
    Machine('easyster_1_8kg', 'Easyster 1.8KG', 'Easyster'),
    Machine('easyster_15kg', 'Easyster 15KG', 'Easyster'),
    Machine('easyster_2_8kg', 'Easyster 2.8KG', 'Easyster'),
    Machine('easyster_30kg', 'Easyster 30KG', 'Easyster'),
    Machine('easyster_4kg', 'Easyster 4KG', 'Easyster'),
    Machine('easyster_800g', 'Easyster 800G', 'Easyster'),
    Machine('easyster_8kg', 'Easyster 8KG', 'Easyster'),
    Machine('fabrica_coffeum_cr1', 'Fabrica Coffeum Cr1', 'Fabrica'),
    Machine('fabrica_coffeum_cr2', 'Fabrica Coffeum Cr2', 'Fabrica'),
    Machine('fabrica_coffeum_cr3', 'Fabrica Coffeum Cr3', 'Fabrica'),
    Machine('fabrica_lion_cr10', 'Fabrica Lion Cr10', 'Fabrica'),
    Machine('fabrica_lion_cr120', 'Fabrica Lion Cr120', 'Fabrica'),
    Machine('fabrica_lion_cr15', 'Fabrica Lion Cr15', 'Fabrica'),
    Machine('fabrica_lion_cr180', 'Fabrica Lion Cr180', 'Fabrica'),
    Machine('fabrica_lion_cr20', 'Fabrica Lion Cr20', 'Fabrica'),
    Machine('fabrica_lion_cr240', 'Fabrica Lion Cr240', 'Fabrica'),
    Machine('fabrica_lion_cr30', 'Fabrica Lion Cr30', 'Fabrica'),
    Machine('fabrica_lion_cr6', 'Fabrica Lion Cr6', 'Fabrica'),
    Machine('fabrica_lion_cr60', 'Fabrica Lion Cr60', 'Fabrica'),
    Machine('fabrica_lion_cr90', 'Fabrica Lion Cr90', 'Fabrica'),
    Machine('fresh_roast_sr300', 'Fresh Roast SR300', 'Fresh Roast'),
    Machine('fresh_roast_sr340', 'Fresh Roast SR340', 'Fresh Roast'),
    Machine('fresh_roast_sr500', 'Fresh Roast SR500', 'Fresh Roast'),
    Machine('fresh_roast_sr700', 'Fresh Roast SR700', 'Fresh Roast'),
    Machine('hearthware_iroast_2', 'Hearthware iRoast 2', 'Hearthware'),
    Machine('hive_roaster_cascabel', 'Hive Roaster Cascabel', 'Hive Roaster'),
    Machine('joper_bpr_25', 'Joper BPR-25', 'Joper'),
    Machine('joper_bsr_15', 'Joper BSR-15', 'Joper'),
    Machine('joper_bsr_3', 'Joper BSR-3', 'Joper'),
    Machine('kaldi_wide', 'Kaldi Wide', 'Kaldi'),
    Machine('kaldi_wide_400', 'Kaldi Wide 400', 'Kaldi'),
    Machine('kaleido_m1', 'Kaleido Sniper M1', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m1', 'Kaleido Sniper M1 Lite', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m1', 'Kaleido Sniper M1 Pro', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m10', 'Kaleido Sniper M10', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m10', 'Kaleido Sniper M10 Pro', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m10', 'Kaleido Sniper M10S / M10 Dual System', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m1', 'Kaleido Sniper M1S / M1 Dual System', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m2', 'Kaleido Sniper M2', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m2', 'Kaleido Sniper M2 Lite', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m2', 'Kaleido Sniper M2 Pro', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m2', 'Kaleido Sniper M2S / M2 Dual System', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m6', 'Kaleido Sniper M6', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m6', 'Kaleido Sniper M6 Pro', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kaleido_m6', 'Kaleido Sniper M6S / M6 Dual System', 'Kaleido', mechanism_family='kaleido_drum'),
    Machine('kraffe_dimi', 'Kraffe Dimi', 'Kraffe'),
    Machine('kraffe_krf_03', 'Kraffe KRF-03', 'Kraffe'),
    Machine('kraffe_krf_06', 'Kraffe KRF-06', 'Kraffe'),
    Machine('kraffe_krf_12', 'Kraffe KRF-12', 'Kraffe'),
    Machine('kraffe_krf_120', 'Kraffe KRF-120', 'Kraffe'),
    Machine('kraffe_krf_15', 'Kraffe KRF-15', 'Kraffe'),
    Machine('kraffe_krf_180', 'Kraffe KRF-180', 'Kraffe'),
    Machine('kraffe_krf_20', 'Kraffe KRF-20', 'Kraffe'),
    Machine('kraffe_krf_240', 'Kraffe KRF-240', 'Kraffe'),
    Machine('kraffe_krf_30', 'Kraffe KRF-30', 'Kraffe'),
    Machine('kraffe_krf_60', 'Kraffe KRF-60', 'Kraffe'),
    Machine('kraffe_primi', 'Kraffe Primi', 'Kraffe'),
    Machine('loring_s15_falcon', 'Loring S15 Falcon', 'Loring'),
    Machine('loring_s35_kestrel', 'Loring S35 Kestrel', 'Loring'),
    Machine('loring_s7_nighthawk', 'Loring S7 Nighthawk', 'Loring'),
    Machine('loring_s70_peregrine', 'Loring S70 Peregrine', 'Loring'),
    Machine('nesco_cr_1010_pr', 'Nesco CR-1010-PR', 'Nesco'),
    Machine('nor_a3prime_a5prime_a10prime_a20prime', 'NOR A3Prime / A5Prime / A10Prime / A20Prime', 'NOR'),
    Machine('nor_curve1_curve3_curve5_curve10', 'NOR Curve1 / Curve3 / Curve5 / Curve10', 'NOR'),
    Machine('nor_n1000i_a1000i', 'NOR N1000i / A1000i', 'NOR'),
    Machine('nor_n10ki_a10ki', 'NOR N10Ki / A10Ki', 'NOR'),
    Machine('nor_n15ki', 'NOR N15Ki', 'NOR'),
    Machine('nor_n2000i_a2000i', 'NOR N2000i / A2000i', 'NOR'),
    Machine('nor_n20ki_a20ki', 'NOR N20Ki / A20Ki', 'NOR'),
    Machine('nor_n3000i_a3000i', 'NOR N3000i / A3000i', 'NOR'),
    Machine('nor_n30ki_a30ki', 'NOR N30Ki / A30Ki', 'NOR'),
    Machine('nor_n5000i_a5000i', 'NOR N5000i / A5000i', 'NOR'),
    Machine('nor_n500i_a500i', 'NOR N500i / A500i', 'NOR'),
    Machine('nor_n50ki_a50ki', 'NOR N50Ki / A50Ki', 'NOR'),
    Machine('norkit', 'Norkit', 'NOR'),
    Machine('petroncini_tm_modular_roaster', 'Petroncini TM Modular Roaster', 'Petroncini'),
    Machine('petroncini_tmr_720', 'Petroncini TMR 720', 'Petroncini'),
    Machine('pratter_1_5', 'PRATTER 1.5', 'Pratter'),
    Machine('pratter_12_0', 'PRATTER 12.0', 'Pratter'),
    Machine('pratter_3_0', 'PRATTER 3.0', 'Pratter'),
    Machine('pratter_30_0', 'PRATTER 30.0', 'Pratter'),
    Machine('pratter_5_0', 'PRATTER 5.0', 'Pratter'),
    Machine('proaster_taehwan_thcr_01', 'Proaster (Taehwan) THCR-01', 'Proaster (Taehwan)'),
    Machine('proaster_taehwan_thcr_03', 'Proaster (Taehwan) THCR-03', 'Proaster (Taehwan)'),
    Machine('proaster_taehwan_thcr_06', 'Proaster (Taehwan) THCR-06', 'Proaster (Taehwan)'),
    Machine('proaster_taehwan_thcr_12', 'Proaster (Taehwan) THCR-12', 'Proaster (Taehwan)'),
    Machine('proaster_taehwan_thcr_120', 'Proaster (Taehwan) THCR-120', 'Proaster (Taehwan)'),
    Machine('proaster_taehwan_thcr_25', 'Proaster (Taehwan) THCR-25', 'Proaster (Taehwan)'),
    Machine('probat_g45_g60_g75_g90_g120', 'Probat G45 / G60 / G75 / G90 / G120', 'Probat'),
    Machine('probat_p01_iii', 'Probat P01 III', 'Probat'),
    Machine('probat_p05_iii', 'Probat P05 III', 'Probat'),
    Machine('probat_p12_iii', 'Probat P12 III', 'Probat'),
    Machine('probat_p25_iii', 'Probat P25 III', 'Probat'),
    Machine('probat_ug15_ug22', 'Probat UG15 / UG22', 'Probat'),
    Machine('probatone_5_12_25', 'Probatone 5 / 12 / 25', 'Probat'),
    Machine('quest_m6', 'Quest M6', 'Quest'),
    Machine('roest_l200_plus', 'ROEST L200 Plus', 'ROEST'),
    Machine('roest_l200_ultra', 'ROEST L200 Ultra', 'ROEST'),
    Machine('roest_s200', 'ROEST S200', 'ROEST'),
    Machine('san_franciscan_sf_1', 'San Franciscan SF-1', 'San Franciscan'),
    Machine('san_franciscan_sf_10', 'San Franciscan SF-10', 'San Franciscan'),
    Machine('san_franciscan_sf_25', 'San Franciscan SF-25', 'San Franciscan'),
    Machine('san_franciscan_sf_6_sf_super_6', 'San Franciscan SF-6 / SF-Super 6', 'San Franciscan'),
    Machine('san_franciscan_sf_75', 'San Franciscan SF-75', 'San Franciscan'),
    Machine('sonofresco_cr1s', 'Sonofresco CR1S', 'Sonofresco'),
    Machine('sonofresco_cr2_cr2_2100', 'Sonofresco CR2 / CR2-2100', 'Sonofresco'),
    Machine('toper_cafemino', 'Toper Cafemino', 'Toper'),
    Machine('toper_tkm_sx_3_sx_5_sx_10_sx_15_sx_20', 'Toper TKM SX-3 / SX-5 / SX-10 / SX-15 / SX-20', 'Toper'),
    Machine('toper_tkm_sx_20', 'Toper TKM-SX 20', 'Toper'),
    Machine('toper_tkm_sx_3e_tkm_sx_5e', 'Toper TKM-SX 3E / TKM-SX 5E', 'Toper'),
    Machine('toper_tkm_sx_5', 'Toper TKM-SX 5', 'Toper'),
    Machine('toper_tkm_x_10', 'Toper TKM-X 10', 'Toper'),
    Machine('toper_tkm_x_15', 'Toper TKM-X 15', 'Toper'),
    Machine('toper_tkm_x_30', 'Toper TKM-X 30', 'Toper'),
)

# ---------------------------------------------------------------------------
# HAND-WRITTEN SUPPLEMENT -- home roasters Artisan's catalogue omits.
# ---------------------------------------------------------------------------
HOME_ROASTER_SUPPLEMENT: tuple[Machine, ...] = (
    Machine('behmor_1600_plus', 'Behmor 1600 Plus', 'Behmor'),
    Machine('behmor_2000ab', 'Behmor 2000AB', 'Behmor'),
    Machine('gene_cafe_cbr_101', 'Gene Café CBR-101', 'Gene Café'),
    Machine('gene_cafe_cbr_1200', 'Gene Café CBR-1200', 'Gene Café'),
    Machine('quest_m3', 'Quest M3', 'Quest'),
    Machine('quest_m3s', 'Quest M3s', 'Quest'),
    # Kaffelogic's own site now sells this as "Nano 7e" (the 220-240V/CE
    # build; electronically identical). Both strings are in circulation.
    Machine('kaffelogic_nano_7', 'Kaffelogic Nano 7', 'Kaffelogic',
            artisan_strings=('Kaffelogic Nano 7', 'Kaffelogic Nano 7e')),
    Machine('sandbox_smart_r1', 'Sandbox Smart R1', 'Sandbox Smart'),
    Machine('sandbox_smart_r2', 'Sandbox Smart R2', 'Sandbox Smart'),
    Machine('cormorant_cr600', 'Cormorant CR600', 'Cormorant'),
    Machine('cormorant_cr900', 'Cormorant CR900', 'Cormorant'),
    Machine('huky_500', 'Huky 500', 'Huky'),
    Machine('skywalker', 'Skywalker', 'Skywalker'),
    Machine('fresh_roast_sr540', 'Fresh Roast SR540', 'Fresh Roast'),
    Machine('fresh_roast_sr800', 'Fresh Roast SR800', 'Fresh Roast'),
    Machine('sonofresco', 'Sonofresco', 'Sonofresco'),
    Machine('nesco', 'Nesco', 'Nesco'),
    Machine('popcorn_popper', "Popcorn popper (Sweet Maria's-style / generic)", 'Generic'),
    Machine('whirley_pop', 'Whirley-Pop / stovetop', 'Generic'),
)


# Picker-only blocks carry no authoritative key of their own, so they take
# whatever ingest would derive from their display_name -- which is what
# makes a Settings pick and an ingested roast land on the same key.
def _with_derived_keys(machines: tuple[Machine, ...]) -> tuple[Machine, ...]:
    return tuple(
        Machine(effective_key(m.display_name), m.display_name, m.manufacturer,
                m.artisan_strings, effective_family(m.display_name))
        for m in machines
    )


MACHINES: tuple[Machine, ...] = (
    ARTISAN_MACHINES
    + _with_derived_keys(RESEARCHED_MACHINES)
    + _with_derived_keys(HOME_ROASTER_SUPPLEMENT)
)

# Built once at import time; find_by_roastertype is on the ingest hot path
# (called once per file via alog/machine.py) so it shouldn't rescan the
# whole tuple on every lookup. First entry wins for a (rare,
# case-insensitive) duplicate string across two Machine rows -- which is why
# ARTISAN_MACHINES comes first in MACHINES: a string Artisan actually writes
# must resolve to the entry that claims it, not to a picker-only row that
# happens to share the text.
_BY_MATCH_STRING: dict[str, Machine] = {}
for _m in MACHINES:
    for _s in _m.match_strings:
        _BY_MATCH_STRING.setdefault(_s.strip().lower(), _m)
del _m, _s


def find_by_roastertype(raw: str) -> Machine | None:
    """Case-insensitive exact match of `raw` (a .alog `roastertype` value)
    against every string the catalogue claims. Exact, not a
    substring/heuristic match -- for ARTISAN_MACHINES that's sound because
    `artisan_strings` are the literal strings Artisan itself writes."""
    if not raw:
        return None
    return _BY_MATCH_STRING.get(raw.strip().lower())


def list_machines() -> list[Machine]:
    """The full catalogue, sorted by manufacturer then model for a
    Settings/search picker."""
    return sorted(MACHINES, key=lambda m: (m.manufacturer.lower(),
                                           m.display_name.strip().lower()))
