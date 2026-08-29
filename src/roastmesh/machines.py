"""Roaster machine catalogue: a search facet and the Settings machine picker.

Two blocks, kept clearly separate:

1. `GENERATED_MACHINES` -- seeded character-for-character from Artisan's own
   machine-setup files (`src/includes/Machines/<Manufacturer>/<Model>.aset`,
   one `roastertype_setup=<string>` per file). That string is exactly what
   Artisan writes into a profile's `roastertype` field, so matching against
   it (`find_by_roastertype`) is exact, not heuristic. Regenerate with
   `tools/build_machine_catalogue.py <path-to-a-local-Machines-dir>`; the
   output below is committed and nothing here talks to the network or
   expects an Artisan checkout at runtime.

2. `HOME_ROASTER_SUPPLEMENT` -- hand-written, and deliberately kept apart
   from the generated block above. Artisan's own catalogue only covers
   connected/commercial roasters -- verified during planning that Behmor,
   Gene Café, Quest, Kaffelogic, Sandbox Smart, Cormorant, Huky, Skywalker,
   Fresh Roast, Sonofresco, Nesco and plain popcorn-popper/stovetop rigs are
   *all* absent from it, and those are exactly the machines this project's
   home-roasting users actually own. Entries can be appended to this list
   freely -- there is no generation step to rerun for it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Machine:
    key: str             # matches roasts.machine_key's vocabulary
    display_name: str    # shown to humans; for GENERATED_MACHINES, the exact roastertype string
    manufacturer: str


def slugify(text: str) -> str:
    """Same slugification alog/machine.py's own fallback uses, so catalogue
    keys stay consistent with what an unrecognized roastertype string has
    always produced."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "unknown"


# ---------------------------------------------------------------------------
# GENERATED -- do not hand-edit. Regenerate with:
#     .venv/bin/python tools/build_machine_catalogue.py <path-to-Machines-dir>
# 254 unique roastertype strings across 90 manufacturers, drawn from the 259
# of Artisan's 262 `.aset` files that carry a `roastertype_setup` key (3 do
# not and are skipped). A few strings carry a stray leading space or an
# unusual manufacturer split (e.g. "Probat G/UG" shipping under both
# "Probat" and "Kirsch+Mausser") -- kept verbatim from source rather than
# "fixed", since exactness against Artisan's own string is the entire point.
# ---------------------------------------------------------------------------
GENERATED_MACHINES: tuple[Machine, ...] = (
    Machine('aillio_bullet_r1', 'Aillio Bullet R1', 'Aillio'),
    Machine('aillio_bullet_r1_ibts', 'Aillio Bullet R1 IBTS', 'Aillio'),
    Machine('aillio_bullet_r2', 'Aillio Bullet R2', 'Aillio'),
    Machine('ambex_ym', 'Ambex YM', 'Ambex'),
    Machine('arc_800', 'Arc 800', 'Arc'),
    Machine('arc_800_rtd', 'ARC 800 RTD', 'Arc'),
    Machine('arc_s', 'Arc S', 'Arc'),
    Machine('arc_s_rtd', 'ARC S RTD', 'Arc'),
    Machine('atilla_gold', 'Atilla GOLD', 'Atilla'),
    Machine('atilla_gold_ii', 'Atilla GOLD II', 'Atilla'),
    Machine('atilla_gold_plus_ii_control', 'Atilla GOLD plus II Control', 'Atilla'),
    Machine('atilla_gold_plus_ii_control_auto', 'Atilla GOLD plus II Control Auto', 'Atilla'),
    Machine('bc_roaster', 'BC Roaster', 'BC'),
    Machine('beango_cube_x', 'BeanGo Cube X', 'BeanGo Cube'),
    Machine('bella_tw', 'Bella TW', 'BellaTW'),
    Machine('berto_autonics_control', 'Berto Autonics Control', 'Berto'),
    Machine('berto_d', 'Berto D', 'Berto'),
    Machine('berto_essential', 'Berto Essential', 'Berto'),
    Machine('berto_one', 'Berto One', 'Berto'),
    Machine('berto_r', 'Berto R', 'Berto'),
    Machine('besca_bee', 'Besca Bee', 'Besca'),
    Machine('besca_bee_v2', 'Besca Bee v2', 'Besca'),
    Machine('besca_bsc_auto', 'Besca BSC auto', 'Besca'),
    Machine('besca_bsc_full_auto', 'Besca BSC full-auto', 'Besca'),
    Machine('besca_bsc_manual_v1', 'Besca BSC manual v1', 'Besca'),
    Machine('besca_bsc_manual_v2', 'Besca BSC manual v2', 'Besca'),
    Machine('bideli_roaster', 'Bideli Roaster', 'Bideli'),
    Machine('blueking_bk', 'BlueKing BK', 'BlueKing'),
    Machine('b_hler_rm_20_playone', 'Bühler RM 20 Playone', 'Bühler'),
    Machine('b_hler_rm_20_simatic', 'Bühler RM 20 Simatic', 'Bühler'),
    Machine('b_hler_rm_20_simatic_legacy', 'Bühler RM 20 Simatic Legacy', 'Bühler'),
    Machine('b_hler_rm_60_240', 'Bühler RM 60-240', 'Bühler'),
    Machine('caparao_plc', 'Caparao PLC', 'Caparao'),
    Machine('carmomaq_caloratto', 'Carmomaq Caloratto', 'Carmomaq'),
    Machine('carmomaq_caloratto_materattor_legacy', 'Carmomaq Caloratto/Materattor Legacy', 'Carmomaq'),
    Machine('carmomaq_masteratto', 'Carmomaq Masteratto', 'Carmomaq'),
    Machine('carmomaq_speciatto', 'Carmomaq Speciatto', 'Carmomaq'),
    Machine('carmomaq_stratto', 'Carmomaq Stratto', 'Carmomaq'),
    Machine('carmomaq_stratto_lab', 'Carmomaq Stratto Lab', 'Carmomaq'),
    Machine('coffed_sr15_automatic', 'Coffed SR15 automatic', 'Coffed'),
    Machine('coffed_sr15_manual_delta', 'Coffed SR15 manual delta', 'Coffed'),
    Machine('coffed_sr25', 'Coffed SR25', 'Coffed'),
    Machine('coffed_sr3_manual', 'Coffed SR3 manual', 'Coffed'),
    Machine('coffed_sr3_manual_delta', 'Coffed SR3 manual delta', 'Coffed'),
    Machine('coffed_sr3_manual_delta_ebm_papst', 'Coffed SR3 manual delta+ EBM-Papst', 'Coffed'),
    Machine('coffed_sr3_manual_delta_honeywell', 'Coffed SR3 manual delta+ Honeywell', 'Coffed'),
    Machine('coffed_sr5_automatic', 'Coffed SR5 automatic', 'Coffed'),
    Machine('coffed_sr5_manual', 'Coffed SR5 manual', 'Coffed'),
    Machine('coffed_sr5_manual_delta', 'Coffed SR5 manual delta', 'Coffed'),
    Machine('coffed_sr5_manual_delta_ebm_papst', 'Coffed SR5 manual delta+ EBM-Papst', 'Coffed'),
    Machine('coffed_sr5_manual_delta_honeywell', 'Coffed SR5 manual delta+ Honeywell', 'Coffed'),
    Machine('coffed_sr60', 'Coffed SR60', 'Coffed'),
    Machine('cms_1', 'CMS-1', 'Coffee Machines Sale'),
    Machine('cms_6_30', 'CMS-6-30', 'Coffee Machines Sale'),
    Machine('cte_fz_94', 'CTE FZ-94', 'Coffee-Tech'),
    Machine('cte_fz94_evo', 'CTE FZ94 EVO', 'Coffee-Tech'),
    Machine('cte_ghibli', 'CTE Ghibli', 'Coffee-Tech'),
    Machine('cte_ghibli_touch', 'CTE Ghibli Touch', 'Coffee-Tech'),
    Machine('cte_silon_touch', 'CTE Silon Touch', 'Coffee-Tech'),
    Machine('cte_silon_usb', 'CTE Silon USB', 'Coffee-Tech'),
    Machine('coffeetool', 'Coffeetool', 'Coffeetool'),
    Machine('cogen_series_c', 'Cogen Series C', 'Cogen'),
    Machine('cogen_series_c_v2', 'Cogen Series C v2', 'Cogen'),
    Machine('craftsmith_craft', 'Craftsmith Craft', 'Craftsmith'),
    Machine('craftsmith_craft_air', 'Craftsmith Craft air', 'Craftsmith'),
    Machine('craftsmith_diy', 'Craftsmith DIY', 'Craftsmith'),
    Machine('d_tgen_dr', 'Dätgen DR', 'Daetgen'),
    Machine('d_tgen_dw', 'Dätgen DW', 'Daetgen'),
    Machine('diedrich_4_sensor', 'Diedrich 4-Sensor', 'Diedrich'),
    Machine('diedrich_6_sensor', 'Diedrich 6-Sensor', 'Diedrich'),
    Machine('diedrich_6_sensor_pre_2018', 'Diedrich 6-Sensor (Pre-2018)', 'Diedrich'),
    Machine('diedrich_cr', 'Diedrich CR', 'Diedrich'),
    Machine('diedrich_dr', 'Diedrich DR', 'Diedrich'),
    Machine('dongyi_br', 'Dongyi BR', 'Dongyi'),
    Machine('dongyi_by', 'Dongyi BY', 'Dongyi'),
    Machine('dongyi_dy', 'Dongyi DY', 'Dongyi'),
    Machine('dmr15_a', 'DMR15-A', 'Dutch Master Roaster'),
    Machine('dmr5_a', 'DMR5-A', 'Dutch Master Roaster'),
    Machine('easyster_airpressure', ' Easyster AirPressure', 'Easyster'),
    Machine('easyster', 'Easyster', 'Easyster'),
    Machine('easyster_3temp', 'Easyster 3Temp', 'Easyster'),
    Machine('easyster_smart', 'Easyster Smart', 'Easyster'),
    Machine('fabrica', 'Fabrica', 'Fabrica'),
    Machine('froco_advanced', 'Froco Advanced', 'Froco'),
    Machine('froco_improved', 'Froco Improved', 'Froco'),
    Machine('garanti_gkpx', 'Garanti GKPX', 'Garanti'),
    Machine('giesen_gpe', 'Giesen GPE', 'Giesen'),
    Machine('giesen_w140a_v1', 'Giesen W140A v1', 'Giesen'),
    Machine('giesen_w15a', 'Giesen W15A', 'Giesen'),
    Machine('giesen_w15e', 'Giesen W15E', 'Giesen'),
    Machine('giesen_w1a', 'Giesen W1A', 'Giesen'),
    Machine('giesen_w1e', 'Giesen W1E', 'Giesen'),
    Machine('giesen_w30a', 'Giesen W30A', 'Giesen'),
    Machine('giesen_w30a_pro', 'Giesen W30A PRO', 'Giesen'),
    Machine('giesen_w45a', 'Giesen W45A', 'Giesen'),
    Machine('giesen_w60a', 'Giesen W60A', 'Giesen'),
    Machine('giesen_w6a', 'Giesen W6A', 'Giesen'),
    Machine('giesen_w6a_pro', 'Giesen W6A PRO', 'Giesen'),
    Machine('giesen_w6e', 'Giesen W6E', 'Giesen'),
    Machine('giesen_wpg', 'Giesen WPG', 'Giesen'),
    Machine('giesen_wxa', 'Giesen WxA', 'Giesen'),
    Machine('giesen_wxa_coarse', 'Giesen WxA coarse', 'Giesen'),
    Machine('giesen_wxa_ir', 'Giesen WxA IR', 'Giesen'),
    Machine('giesen_wxa_ir_env', 'Giesen WxA IR Env', 'Giesen'),
    Machine('giesen_wxa', 'Giesen WxA+', 'Giesen'),
    Machine('giesen_wxa_ir', 'Giesen WxA+ IR', 'Giesen'),
    Machine('giesen_wxa_ir_env', 'Giesen WxA+ IR Env', 'Giesen'),
    Machine('gr_2xemko', 'GR 2xEMKO', 'Golden Roasters'),
    Machine('gr_automatic', 'GR Automatic', 'Golden Roasters'),
    Machine('gr_delta', 'GR Delta', 'Golden Roasters'),
    Machine('gr_legacy', 'GR Legacy', 'Golden Roasters'),
    Machine('gr_manual', 'GR Manual', 'Golden Roasters'),
    Machine('has_garanti_hgs', 'Has Garanti HGS', 'Has Garanti'),
    Machine('has_garanti_hsr', 'Has Garanti HSR', 'Has Garanti'),
    Machine('hb_model_s', 'HB Model S', 'HB'),
    Machine('hb_standard', 'HB Standard', 'HB'),
    Machine('hive_roaster_data_dome', 'Hive Roaster Data Dome', 'Hive Roaster'),
    Machine('hottop_2k', 'Hottop 2K+', 'Hottop'),
    Machine('hottop_tc4', 'Hottop TC4', 'Hottop'),
    Machine('ikawa_home', 'IKAWA HOME', 'IKAWA'),
    Machine('ikawa_pro', 'IKAWA PRO', 'IKAWA'),
    Machine('ikawa_pro_x', 'IKAWA PRO X', 'IKAWA'),
    Machine('imf_rm', 'IMF RM', 'IMF'),
    Machine('imf_rm_auto', 'IMF RM Auto', 'IMF'),
    Machine('imf_rm_control', 'IMF RM Control', 'IMF'),
    Machine('imf_rm_legacy', 'IMF RM legacy', 'IMF'),
    Machine('irm_series_mitsubishi', 'iRm Series Mitsubishi', 'iRm Series'),
    Machine('irm_series_omron', 'iRm Series Omron', 'iRm Series'),
    Machine('joper_plc', 'Joper PLC', 'Joper'),
    Machine('kaldi_fortis', 'Kaldi Fortis', 'Kaldi'),
    Machine('kaleido_legacy', 'Kaleido Legacy', 'Kaleido'),
    Machine('kaleido_network', 'Kaleido Network', 'Kaleido'),
    Machine('kaleido_serial', 'Kaleido Serial', 'Kaleido'),
    Machine('kapok', 'KapoK', 'KapoK'),
    Machine('kapok_inlet', 'KapoK Inlet', 'KapoK'),
    Machine('probat_g_ug', 'Probat G/UG', 'Kirsch+Mausser'),
    Machine('probat_g_ug_control', 'Probat G/UG control', 'Kirsch+Mausser'),
    Machine('kraffe_plc', 'Kraffe PLC', 'Kraffe'),
    Machine('kuban_supreme_automatic', 'Kuban Supreme Automatic', 'Kuban'),
    Machine('kuban_supreme_manual', 'Kuban Supreme Manual', 'Kuban'),
    Machine('lilla', 'Lilla', 'Lilla'),
    Machine('loring', 'Loring', 'Loring'),
    Machine('loring_auto', 'Loring Auto', 'Loring'),
    Machine('mcr_digital_control_panel_1000', 'MCR Digital Control Panel 1000', 'Mill City Roasters'),
    Machine('mcr_digital_control_panel_1000_c', 'MCR Digital Control Panel 1000 C', 'Mill City Roasters'),
    Machine('mcr_digital_control_panel_1200_c', 'MCR Digital Control Panel 1200 C', 'Mill City Roasters'),
    Machine('mcr_phidget', 'MCR Phidget', 'Mill City Roasters'),
    Machine('mcr_phidget_delta_controls_port_on_the_back', 'MCR Phidget & Delta controls (port on the back)', 'Mill City Roasters'),
    Machine('mcr_phidget_delta_controls_port_on_the_right', 'MCR Phidget & Delta controls (port on the right)', 'Mill City Roasters'),
    Machine('mcr_phidget_delta_controls_port_on_the_right_c', 'MCR Phidget & Delta controls (port on the right) C', 'Mill City Roasters'),
    Machine('mcr_phidget_shihlin_controls_port_on_the_back', 'MCR Phidget & Shihlin controls (port on the back)', 'Mill City Roasters'),
    Machine('mcr_standard_control_panel_delta', 'MCR Standard Control Panel (Delta)', 'Mill City Roasters'),
    Machine('mcr_standard_control_panel_delta_c', 'MCR Standard Control Panel (Delta) C', 'Mill City Roasters'),
    Machine('mcr_standard_control_panel_fotek', 'MCR Standard Control Panel (Fotek)', 'Mill City Roasters'),
    Machine('mcr_standard_control_panel_fotek_c', 'MCR Standard Control Panel (Fotek) C', 'Mill City Roasters'),
    Machine('mugma_1000', 'Mugma 1000', 'Mugma'),
    Machine('mugma_2000', 'Mugma 2000', 'Mugma'),
    Machine('neuhaus_neotec_neoroast', 'Neuhaus Neotec Neoroast', 'Neuhaus Neotec'),
    Machine('neuhaus_neotec_rfb', 'Neuhaus Neotec RFB', 'Neuhaus Neotec'),
    Machine('nor_a_series', 'NOR A Series', 'NOR'),
    Machine('nor_extension_modbus', 'NOR Extension MODBUS', 'NOR'),
    Machine('nor_n_series', 'NOR N Series', 'NOR'),
    Machine('nordic_delta_dta', 'Nordic Delta DTA', 'Nordic'),
    Machine('nordic_delta_dtk', 'Nordic Delta DTK', 'Nordic'),
    Machine('nordic_plc', 'Nordic PLC', 'Nordic'),
    Machine('north_standard_control_panel_fotek', 'North Standard Control Panel (Fotek)', 'North'),
    Machine('north_standard_control_panel_fotek_c', 'North Standard Control Panel (Fotek) C', 'North'),
    Machine('opp_mr', 'Opp MR', 'Opp'),
    Machine('orbiter_ob_1', 'Orbiter OB-1', 'Orbiter'),
    Machine('otesla', 'OTesla', 'OTesla'),
    Machine('zt_rk_oks', 'Öztürk OKS', 'Ozturk'),
    Machine('petroncini_asem', 'Petroncini ASEM', 'Petroncini'),
    Machine('petroncini_maestro', 'Petroncini Maestro', 'Petroncini'),
    Machine('petroncini_maestro_i06', 'Petroncini Maestro i06', 'Petroncini'),
    Machine('petroncini_traditional', 'Petroncini Traditional', 'Petroncini'),
    Machine('phidget_2xrtd', 'Phidget 2xRTD', 'Phidget'),
    Machine('phidget_2xtc', 'Phidget 2xTC', 'Phidget'),
    Machine('phidget_databridge', 'Phidget Databridge', 'Phidget'),
    Machine('phoenix_oro_pxf', 'Phoenix ORO PXF', 'Phoenix'),
    Machine('phoenix_roaster', 'Phoenix Roaster', 'Phoenix'),
    Machine('plugin_roast', 'Plugin Roast', 'Plugin'),
    Machine('pratter_autonics', 'Pratter Autonics', 'Pratter'),
    Machine('pratter_plc', 'Pratter PLC', 'Pratter'),
    Machine('primo_xr', 'Primo Xr', 'Primo'),
    Machine('prisma_plc', 'Prisma PLC', 'Prisma'),
    Machine('prisma_usb', 'Prisma USB', 'Prisma'),
    Machine('proaster', 'Proaster', 'Proaster'),
    Machine('proaster_3temp', 'Proaster 3Temp', 'Proaster'),
    Machine('proaster_airpressure', 'Proaster AirPressure', 'Proaster'),
    Machine('proaster_thcr_01a', 'Proaster THCR-01A', 'Proaster'),
    Machine('probat_g_ug_websockets', ' Probat G/UG WebSockets', 'Probat'),
    Machine('probat_p_series', 'Probat P Series', 'Probat'),
    Machine('probat_sample', 'Probat Sample', 'Probat'),
    Machine('probatone', 'Probatone', 'Probat'),
    Machine('prometheus_ignis', 'Prometheus Ignis', 'Prometheus'),
    Machine('r_r_r_rv_automatic', 'R&R R/RV Automatic', 'R & R'),
    Machine('r_r_r_rv_manual', 'R&R R/RV Manual', 'R & R'),
    Machine('rasco_mac_rm', 'Rasco Mac RM', 'Rasco Mac'),
    Machine('roastmax', 'Roastmax', 'Roastmax'),
    Machine('roest_100', 'ROEST 100', 'ROEST'),
    Machine('roest_200', 'ROEST 200', 'ROEST'),
    Machine('roest_p3000', 'ROEST P3000', 'ROEST'),
    Machine('rolltech_el', 'Rolltech EL', 'Rolltech'),
    Machine('san_franciscan', 'San Franciscan', 'San Franciscan'),
    Machine('san_franciscan_eurotherm', 'San Franciscan Eurotherm', 'San Franciscan'),
    Machine('santoker_1xpxr', 'Santoker 1xPXR', 'Santoker'),
    Machine('santoker_2xpxf', 'Santoker 2xPXF', 'Santoker'),
    Machine('santoker_2xpxr', 'Santoker 2xPXR', 'Santoker'),
    Machine('santoker_cube_bt', 'Santoker Cube BT', 'Santoker'),
    Machine('santoker_cube_pid', 'Santoker Cube PID', 'Santoker'),
    Machine('santoker_q_x_series_bt', 'Santoker Q + X Series BT', 'Santoker'),
    Machine('santoker_q_x_series_wifi', 'Santoker Q + X Series WiFi', 'Santoker'),
    Machine('santoker_r_master_series_bt', 'Santoker R Master Series BT', 'Santoker'),
    Machine('santoker_r_master_series_wifi', 'Santoker R Master Series WiFi', 'Santoker'),
    Machine('santoker_r_series_bt', 'Santoker R Series BT', 'Santoker'),
    Machine('santoker_r_series_usb', 'Santoker R Series USB', 'Santoker'),
    Machine('schuilenburg_plc', 'Schuilenburg PLC', 'Schuilenburg'),
    Machine('sedona_elite', 'Sedona Elite', 'Sedona Elite'),
    Machine('sedona_elite_2in1', 'Sedona Elite 2in1', 'Sedona Elite'),
    Machine('sedona_elite_pxf', 'Sedona Elite PXF', 'Sedona Elite'),
    Machine('susa', 'SUSA', 'SEVVALUSA'),
    Machine('sivetz_srm', 'Sivetz SRM', 'Sivetz'),
    Machine('sivetz_srm_legacy', 'Sivetz SRM legacy', 'Sivetz'),
    Machine('sweet_coffee_italia_gemma_26_30ind', 'Sweet Coffee Italia – Gemma 26-30IND', 'Sweet Coffee Italia'),
    Machine('sweet_coffee_italia_gemma_2ind', 'Sweet Coffee Italia – Gemma 2IND', 'Sweet Coffee Italia'),
    Machine('sweet_coffee_italia_gemma_6_8ind', 'Sweet Coffee Italia – Gemma 6-8IND', 'Sweet Coffee Italia'),
    Machine('titanium_tgx', 'Titanium TGX', 'Titanium'),
    Machine('toper_tkm_sx', 'Toper TKM-SX', 'Toper'),
    Machine('toper_tkm_sx_control', 'Toper TKM-SX Control', 'Toper'),
    Machine('toper_usb', 'Toper USB', 'Toper'),
    Machine('tostabar_genius', 'Tostabar Genius', 'Tostabar'),
    Machine('trinitas_t2', 'TRINITAS T2', 'TRINITAS'),
    Machine('trinitas_t2_air', 'TRINITAS T2 air', 'TRINITAS'),
    Machine('trinitas_t2_legacy', 'TRINITAS T2 legacy', 'TRINITAS'),
    Machine('trinitas_t7', 'TRINITAS T7', 'TRINITAS'),
    Machine('trinitas_t7_gas', 'TRINITAS T7 gas', 'TRINITAS'),
    Machine('trinitas_t7_legacy', 'TRINITAS T7 legacy', 'TRINITAS'),
    Machine('twino_ozstar', 'Twino/Ozstar', 'Twino'),
    Machine('typhoon_hybrid', 'Typhoon Hybrid', 'Typhoon'),
    Machine('typhoon_shoproaster', 'Typhoon Shoproaster', 'Typhoon'),
    Machine('us_roaster_corp', 'US Roaster Corp', 'US Roaster Corp'),
    Machine('vnt_phidget', 'VNT Phidget', 'VNT'),
    Machine('vnt_pid', 'VNT PID', 'VNT'),
    Machine('vortecs_pro', 'Vortecs Pro', 'Vortecs'),
    Machine('wintop_wb', 'Wintop WB', 'Wintop'),
    Machine('wintop_wk', 'Wintop WK', 'Wintop'),
    Machine('wintop_ws_2in1', 'Wintop WS 2in1', 'Wintop'),
    Machine('wintop_ws_fuji', 'Wintop WS Fuji', 'Wintop'),
    Machine('yangchia_8xxn', 'Yangchia 8xxn', 'Yangchia'),
    Machine('yoshan_br', 'Yoshan BR', 'Yoshan'),
    Machine('yoshan_by', 'Yoshan BY', 'Yoshan'),
    Machine('yoshan_dy', 'Yoshan DY', 'Yoshan'),
    Machine('yoshan_x', 'Yoshan X', 'Yoshan'),
    Machine('yoshan_ys', 'Yoshan YS', 'Yoshan'),
)

# ---------------------------------------------------------------------------
# HAND-WRITTEN SUPPLEMENT -- home roasters Artisan's own catalogue omits
# (it only covers connected/commercial machines). Append freely; there is no
# generation step to keep in sync for this block.
#
# Aillio Bullet R1/R2 and the Hottop KN-8828B-2K+ ("Hottop 2K+") are already
# covered by GENERATED_MACHINES above and are deliberately not duplicated
# here.
# ---------------------------------------------------------------------------
HOME_ROASTER_SUPPLEMENT: tuple[Machine, ...] = (
    Machine('behmor_1600_plus', 'Behmor 1600 Plus', 'Behmor'),
    Machine('behmor_2000ab', 'Behmor 2000AB', 'Behmor'),
    Machine('gene_cafe_cbr_101', 'Gene Café CBR-101', 'Gene Café'),
    Machine('gene_cafe_cbr_1200', 'Gene Café CBR-1200', 'Gene Café'),
    Machine('quest_m3', 'Quest M3', 'Quest'),
    Machine('quest_m3s', 'Quest M3s', 'Quest'),
    Machine('kaffelogic_nano_7', 'Kaffelogic Nano 7', 'Kaffelogic'),
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

# Brand rules that predate this catalogue and own their machine_key outright
# (alog/machine.py's MACHINE_ALIASES and its Kaleido model-number rule).
# Slugifying a display_name is the right key for the ~250 machines nothing
# else claims, but for these brands it invents a *second* vocabulary for a
# machine that already has one: the catalogue would advertise
# "aillio_bullet_r1" in a picker while every Bullet roast ever ingested is
# stored under "aillio_bullet". A user who picked their own machine in
# Settings would then match none of their own roasts, which is precisely
# what the machine facet exists to do. Caught end-to-end, not by a unit
# test -- `profile set --machine aillio_bullet` was rejected as unknown
# while being the only key the index actually contains.
#
# So the catalogue collapses those keys to the pre-existing ones. The
# precise model is not lost: it stays in display_name, which is what
# users.machine_display stores and what a picker shows. Several rows
# therefore share one key (three Bullets, two Hottops), which is correct --
# they are one searchable machine.
#
# test_machines.py pins the invariant that makes this safe: every catalogue
# entry's display_name must normalize back to that entry's own key. The
# duplicated rules below cannot drift from alog/machine.py's without
# failing it.
_ALIAS_KEYS: tuple[tuple[str, str], ...] = (
    ("hottop", "hottop"),
    ("behmor", "behmor"),
    ("bullet", "aillio_bullet"),
)
_KALEIDO_MODEL_RE = re.compile(r"\bm(\d+)\b")


def effective_key(display_name: str) -> str:
    """The machine_key an .alog carrying this exact roastertype would be
    stored under -- i.e. the catalogue's key must be what ingest produces,
    never a parallel vocabulary."""
    text = display_name.strip().lower()
    if "kaleido" in text:
        model = _KALEIDO_MODEL_RE.search(text)
        if model:
            return f"kaleido_m{model.group(1)}"
        return "kaleido_legacy" if "legacy" in text else "kaleido_serial"
    for substring, key in _ALIAS_KEYS:
        if substring in text:
            return key
    return slugify(display_name)


MACHINES: tuple[Machine, ...] = tuple(
    Machine(effective_key(m.display_name), m.display_name, m.manufacturer)
    for m in GENERATED_MACHINES + HOME_ROASTER_SUPPLEMENT
)

# Built once at import time; find_by_roastertype is on the ingest hot path
# (called once per file via alog/machine.py) so it shouldn't rescan the
# whole tuple on every lookup. First entry wins for a (rare, case-insensitive)
# duplicate roastertype string across two Machine rows.
_BY_ROASTERTYPE_LOWER: dict[str, Machine] = {}
for _m in MACHINES:
    _BY_ROASTERTYPE_LOWER.setdefault(_m.display_name.strip().lower(), _m)
del _m


def find_by_roastertype(raw: str) -> Machine | None:
    """Case-insensitive exact match of `raw` (a .alog `roastertype` value)
    against the catalogue's own display_name strings. Exact, not a
    substring/heuristic match -- for GENERATED_MACHINES that's sound because
    display_name *is* the literal string Artisan itself writes into that
    field for that machine."""
    if not raw:
        return None
    return _BY_ROASTERTYPE_LOWER.get(raw.strip().lower())


def list_machines() -> list[Machine]:
    """The full catalogue (generated + supplement), sorted by manufacturer
    then model for a Settings/search picker."""
    return sorted(MACHINES, key=lambda m: (m.manufacturer.lower(), m.display_name.strip().lower()))
