"""Known journal event names for schema-drift awareness.

Events reaching processevent() are counted; on monitor exit, types that are
neither handled nor deliberately ignored are reported so game updates show
up as visible drift instead of silent no-ops.
"""

HANDLED_EVENTS = frozenset({
    "Bounty", "Commander", "Died", "EjectCargo", "FSDJump", "FactionKillBond",
    "FighterDestroyed", "HullDamage", "LaunchFighter", "LoadGame", "Loadout",
    "Location", "MissionAbandoned", "MissionAccepted", "MissionCompleted",
    "MissionFailed", "MissionRedirected", "Missions", "Music", "PowerplayMerits",
    "Progress", "Rank", "ReceiveText", "ReservoirReplenished", "ShieldState",
    "ShipTargeted", "ShipyardSwap", "Shutdown", "SupercruiseDestinationDrop",
    "SupercruiseEntry",
})

# Common events this monitor deliberately does not act on; suppressed from the
# drift report so it stays short and actionable.
IGNORED_EVENTS = frozenset({
    "ApproachCancel", "ApproachSet", "BodyDeparted", "BodyInstalled",
    "CarrierJump", "CarrierJumpRequest", "CellLevel", "CommunityGoal",
    "Creation", "Docked", "DockingCancelled", "DockingComputer",
    "DockingDenied", "DockingRequested", "DockingTimedOut", "DroneCancel",
    "DroneLaunch", "Fileheader", "FlightLaunch", "FuelScoop",
    "FuelScoopReport", "HeatDamage", "HeatWarning", "InternalTrade",
    "JetConeBoost", "JetConeTurn", "LaunchDrones", "LandingGear",
    "MarketAuditLog", "MarketBuy", "MarketExit", "MarketSell", "MultiAdd",
    "MultiExit", "MultiJoin", "MultiLeave", "ModuleBuy", "ModuleCollect",
    "ModuleDrop", "ModuleEngage", "ModulePickup", "ModulePurchase",
    "ModuleRetieve", "ModuleRetrieve", "ModuleSell", "ModuleSellRemote",
    "ModuleStore", "ModuleSwap", "ModuleTransfer", "ModuleUse", "Mutator",
    "NewLeader", "NDCFSDeleted", "NearSurface", "Outfitted", "Passengers",
    "PVPKill", "RebootTimer", "Refuel", "Repair", "RepairDrone", "Resign",
    "Resurrect", "Reward", "SearchAndRescue", "SetUserShipName", "ShieldChg",
    "ShieldDown", "ShieldUp", "ShowShip", "Sortie", "Status",
    "StopRoute", "StoredShips", "StoredMaterials", "StoredModules",
    "SupercruiseExit", "SuitEquipment", "SystemConsolidated",
    "SystemFactionChanged", "TechnologyBroker", "UnderAttack",
    "Undocked", "VehicleSwitch", "Warpaper", "WingAdd", "WingDefect",
    "WingJoin", "WingLeave", "WingManager",
})


def unhandled_types(seen_counts):
    """Event types seen but neither handled nor ignored, busiest first."""
    unhandled = [(count, name) for name, count in seen_counts.items()
                 if name not in HANDLED_EVENTS and name not in IGNORED_EVENTS]
    unhandled.sort(reverse=True)
    return unhandled
