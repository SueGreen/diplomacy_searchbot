# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# import diplomacy
#
# game = diplomacy.Game()
# MAP = game.map
#
#
# MISSPELLINGS = {
#     "SKAGERRACK": "SKAGERRAK",
#     "HELIGOLAND BIGHT": "HELGOLAND BIGHT",
#     "GULF OF LYONS": "GULF OF LYON",
#     "BULGARIA (NORTH COAST)": "BULGARIA (EAST COAST)",
# }
#
# # map from full name to short code, e.g. "Trieste" -> "TRI"
# FULL_TO_SHORT = {}
# for full_name in MAP.loc_name.keys():
#     x = full_name.upper().replace(".", "")
#     FULL_TO_SHORT[full_name] = MAP.loc_name.get(x) or MAP.loc_name[MISSPELLINGS[x]]

FULL_TO_SHORT = {
    "ADRIATIC SEA": "ADR",
    "AEGEAN SEA": "AEG",
    "ALBANIA": "ALB",
    "ANKARA": "ANK",
    "APULIA": "APU",
    "ARMENIA": "ARM",
    "BALTIC SEA": "BAL",
    "BARENTS SEA": "BAR",
    "BELGIUM": "BEL",
    "BERLIN": "BER",
    "BLACK SEA": "BLA",
    "BOHEMIA": "BOH",
    "BREST": "BRE",
    "BUDAPEST": "BUD",
    "BULGARIA (EAST COAST)": "BUL/EC",
    "BULGARIA (SOUTH COAST)": "BUL/SC",
    "BULGARIA": "BUL",
    "BURGUNDY": "BUR",
    "CLYDE": "CLY",
    "CONSTANTINOPLE": "CON",
    "DENMARK": "DEN",
    "EASTERN MEDITERRANEAN": "EAS",
    "EDINBURGH": "EDI",
    "ENGLISH CHANNEL": "ENG",
    "FINLAND": "FIN",
    "GALICIA": "GAL",
    "GASCONY": "GAS",
    "GREECE": "GRE",
    "GULF OF LYON": "LYO",
    "GULF OF BOTHNIA": "BOT",
    "HELGOLAND BIGHT": "HEL",
    "HOLLAND": "HOL",
    "IONIAN SEA": "ION",
    "IRISH SEA": "IRI",
    "KIEL": "KIE",
    "LIVERPOOL": "LVP",
    "LIVONIA": "LVN",
    "LONDON": "LON",
    "MARSEILLES": "MAR",
    "MID-ATLANTIC OCEAN": "MAO",
    "MOSCOW": "MOS",
    "MUNICH": "MUN",
    "NAPLES": "NAP",
    "NORTH ATLANTIC OCEAN": "NAO",
    "NORTH AFRICA": "NAF",
    "NORTH SEA": "NTH",
    "NORWAY": "NWY",
    "NORWEGIAN SEA": "NWG",
    "PARIS": "PAR",
    "PICARDY": "PIC",
    "PIEDMONT": "PIE",
    "PORTUGAL": "POR",
    "PRUSSIA": "PRU",
    "ROME": "ROM",
    "RUHR": "RUH",
    "RUMANIA": "RUM",
    "SERBIA": "SER",
    "SEVASTOPOL": "SEV",
    "SILESIA": "SIL",
    "SKAGERRAK": "SKA",
    "SMYRNA": "SMY",
    "SPAIN (NORTH COAST)": "SPA/NC",
    "SPAIN (SOUTH COAST)": "SPA/SC",
    "SPAIN": "SPA",
    "ST PETERSBURG (NORTH COAST)": "STP/NC",
    "ST PETERSBURG (SOUTH COAST)": "STP/SC",
    "ST PETERSBURG": "STP",
    "SWEDEN": "SWE",
    "SYRIA": "SYR",
    "TRIESTE": "TRI",
    "TUNIS": "TUN",
    "TUSCANY": "TUS",
    "TYROLIA": "TYR",
    "TYRRHENIAN SEA": "TYS",
    "UKRAINE": "UKR",
    "VENICE": "VEN",
    "VIENNA": "VIE",
    "WALES": "WAL",
    "WARSAW": "WAR",
    "WESTERN MEDITERRANEAN": "WES",
    "YORKSHIRE": "YOR",
    "SWITZERLAND": "SWI",
}
