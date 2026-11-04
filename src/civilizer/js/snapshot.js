// Civilizer live snapshot: evaluated inside the Civ VII UI (Coherent/V8) via the
// UI debugger. Returns a JSON string of what the local player can see on screen.
//
// SAFETY: native game bindings do not validate arguments; a wrong call can
// segfault the game (it happened: Stats.getYields with no args). Every call here
// copies a call site in Firaxis's shipped UI JS (path noted in comments), with
// the same argument shapes. Do not add calls without such a precedent.
//
// __OPTIONS__ is replaced by the Python side with a JSON object.
(() => {
  const OPT = __OPTIONS__;
  const errors = [];
  const sec = (name, fn) => {
    try {
      return fn();
    } catch (e) {
      errors.push(`${name}: ${e && e.message ? e.message : e}`);
      return null;
    }
  };
  const loc = (k) => {
    try {
      return k ? Locale.compose(k) : k;
    } catch (e) {
      return k;
    }
  };
  const r1 = (n) => (typeof n === "number" ? Math.round(n * 10) / 10 : n);
  const typeOf = (table, hash, col) => {
    const d = hash != null ? GameInfo[table].lookup(hash) : null;
    return d ? d[col] : null;
  };

  const localID = GameContext.localPlayerID;
  const local = Players.get(localID);
  const localDiplo = local.Diplomacy;
  // Order + types as the UI iterates them (city-details/model-city-details.js)
  const YIELDS = [];
  for (let i = 0; i < GameInfo.Yields.length; i++) YIELDS.push(GameInfo.Yields[i].YieldType);
  const TOPBAR = ["YIELD_GOLD", "YIELD_SCIENCE", "YIELD_CULTURE", "YIELD_HAPPINESS", "YIELD_DIPLOMACY", "YIELD_PRODUCTION", "YIELD_FOOD"];
  const hasMet = (pid) => pid === localID || localDiplo.hasMet(pid);
  const revealed = (x, y) =>
    GameplayMap.getRevealedState(localID, x, y) != RevealedStates.HIDDEN; // lenses / city-banners

  // player-yields-report/model-yields-report.js: getYieldMap(api)
  const netYields = (api) => {
    const o = {};
    for (const y of TOPBAR) o[y] = r1(api.getNetYield(YieldTypes[y]));
    return o;
  };

  const out = { schema: 1, captured_at: new Date().toISOString(), options: OPT };

  // ---------------------------------------------------------------- game
  out.game = sec("game", () => {
    const apm = Game.AgeProgressManager; // legend-progress, age-progression-warning
    return {
      turn: Game.turn,
      seed: Configuration.getGame().gameSeed,
      difficulty: Configuration.getGame().difficultyName,
      age: typeOf("Ages", Game.age, "AgeType"),
      local_player_id: localID,
      age_progress: { current: apm.getCurrentAgeProgressionPoints(), max: apm.getMaxAgeProgressionPoints() },
      crisis_stage_trigger_percent: [0, 1, 2].map((i) => Game.CrisisManager.getCrisisStageTriggerPercent(0, i)), // crisis-meter.js
    };
  });

  // ---------------------------------------------------------------- me (top bar + empire)
  out.me = sec("me", () => {
    const st = local.Stats;
    const me = {
      id: localID,
      leader: loc(GameInfo.Leaders.lookup(local.leaderType)?.Name),
      civilization: loc(local.civilizationFullName),
      // panel-yield-banner.js
      gold_balance: r1(local.Treasury.goldBalance),
      influence_balance: r1(local.DiplomacyTreasury.diplomacyBalance),
      per_turn: netYields(st),
      settlements: st.numSettlements,
      settlement_cap: st.settlementCap,
    };
    // panel-sub-system-dock.js: research + civics
    const tree = (api, treeType) => {
      const t = Game.ProgressionTrees.getTree(localID, treeType);
      if (!t || t.activeNodeIndex < 0) return null;
      const active = t.nodes[t.activeNodeIndex];
      const nd = Game.ProgressionTrees.getNode(localID, active.nodeType);
      const info = GameInfo.ProgressionTreeNodes.lookup(active.nodeType);
      const cost = api.getNodeCost(api.getResearching().type);
      return {
        node: info?.ProgressionTreeNodeType,
        name: loc(info?.Name) + (nd && nd.depthUnlocked >= 1 ? " " + (nd.depthUnlocked + 1) : ""),
        progress: nd ? r1(nd.progress) : null,
        cost: r1(cost),
        turns_left: api.getTurnsLeft(),
      };
    };
    me.researching = sec("me.tech", () => tree(local.Techs, local.Techs.getTreeType()));
    me.civic = sec("me.civic", () => tree(local.Culture, local.Culture.getActiveTree()));
    // policies/model-policies.js
    me.government = sec("me.gov", () => loc(GameInfo.Governments.lookup(local.Culture.getGovernmentType())?.Name));
    const slot = (t) =>
      local.Culture.getActiveTraditions(t).map((p) => loc(GameInfo.Traditions.lookup(p)?.Name));
    me.policies = sec("me.policies", () => slot(CultureSlotTypes.POLICY_CULTURE_SLOT));
    me.traditions = sec("me.traditions", () => slot(CultureSlotTypes.TRADITION_CULTURE_SLOT));
    me.crisis_policies = sec("me.crisis", () => slot(CultureSlotTypes.CRISIS_CULTURE_SLOT));
    me.slots = sec("me.slots", () => ({
      policy: local.Culture.getNumCultureSlots(CultureSlotTypes.POLICY_CULTURE_SLOT),
      tradition: local.Culture.getNumCultureSlots(CultureSlotTypes.TRADITION_CULTURE_SLOT),
      crisis: local.Culture.getNumCultureSlots(CultureSlotTypes.CRISIS_CULTURE_SLOT),
    }));
    // diplo-ribbon / celebration UI
    me.golden_age = sec("me.golden", () =>
      local.Happiness.isInGoldenAge() ? { turns_left: local.Happiness.getGoldenAgeTurnsLeft() } : null
    );
    me.religion = sec("me.religion", () => ({ name: loc(local.Religion.getReligionName()), holy_city: loc(local.Religion.getHolyCityName()) }));
    me.ideology = sec("me.ideology", () => typeOf("Ideologies", local.Culture.getChosenIdeology(), "IdeologyType"));
    // model-yields-report.js constructUnitData: unit maintenance by type
    me.unit_maintenance = sec("me.unitmaint", () => {
      const seen = [], rows = [];
      let total = 0;
      for (const u of local.Units.getUnits()) {
        if (seen.includes(u.type)) continue;
        seen.push(u.type);
        const cost = local.Treasury.getMaintenanceForAllUnitsOfType(u.type);
        total += cost;
        rows.push({ unit: loc(u.name), count: local.Units.getNumUnitsOfType(u.type), gold: r1(-cost) });
      }
      return { total_gold: r1(-total), by_type: rows };
    });
    return me;
  });

  // ---------------------------------------------------------------- my settlements
  const cityNodes = [
    // city-details/model-city-details.js addIncomeHierarchy
    "BUILDING_YIELDS", "IMPROVEMENT_YIELDS", "YIELD_FROM_RESOURCES", "YIELD_FROM_TRADE",
    "YIELD_FROM_DEMAND_FOR_TRADE", "YIELD_FROM_PROJECTS", "YIELD_FROM_GREAT_WORKS",
    "YIELD_FROM_CONNECTED_TOWNS", "YIELD_FROM_SURPLUS_HAPPINESS", "PRODUCTION_CONVERSION_PROJECT",
    "CITY_EFFECTS_YIELDS", "ACTIVE_CIV_TRADITIONS",
  ];
  const deductNodes = ["BUILDING_MAINTENANCE", "HAPPINESS_UPKEEP", "MAINTENANCE_FROM_WORKERS"];

  const cityBreakdown = (cy) => {
    const res = {};
    for (let yi = 0; yi < YIELDS.length; yi++) {
      const parts = {};
      for (const n of cityNodes) {
        if (CityYieldNodes[n] === undefined) continue;
        const v = cy.getYieldsForNode(yi, [CityYieldNodes.INCOME, CityYieldNodes[n]], true);
        if (v && v.value) parts[n.toLowerCase()] = r1(v.value);
      }
      const inc = cy.getYieldSummaryForNode(yi, [CityYieldNodes.INCOME]);
      if (inc && inc.modifier && inc.modifier.value) parts.modifier_pct = r1(inc.modifier.value);
      const ded = cy.getYieldSummaryForNode(yi, [CityYieldNodes.MINUS_DEDUCTIONS]);
      if (ded && ded.value) {
        parts.deductions = r1(ded.value);
        for (const n of deductNodes) {
          if (CityYieldNodes[n] === undefined) continue;
          const v = cy.getYieldsForNode(yi, [CityYieldNodes.MINUS_DEDUCTIONS, CityYieldNodes.DEDUCTIONS, CityYieldNodes[n]], true);
          if (v && v.value) parts["minus_" + n.toLowerCase()] = r1(v.value);
        }
      }
      if (Object.keys(parts).length) res[YIELDS[yi]] = parts;
    }
    return res;
  };

  function ComponentIDMatch(a, b) {
    return a && b && a.owner == b.owner && a.id == b.id && a.type == b.type;
  }

  out.settlements = sec("settlements", () =>
    local.Cities.getCities().map((city) => {
      const c = { name: loc(city.name), id: city.id.id, x: city.location.x, y: city.location.y };
      sec("city.basic", () => {
        Object.assign(c, {
          type: city.isTown ? "town" : "city",
          capital: city.isCapital,
          population: city.population,
          urban_pop: city.urbanPopulation,
          rural_pop: city.ruralPopulation,
          specialists: city.Workers ? city.Workers.getNumWorkers(false) : 0,
          specialist_cap_per_tile: city.Workers ? city.Workers.getCityWorkerCap() : null,
          being_razed: city.isBeingRazed,
          distant_lands: city.isDistantLands,
        });
      });
      c.yields = sec("city.yields", () => {
        const y = {};
        for (const t of YIELDS) y[t] = r1(city.Yields.getNetYield(t));
        return y;
      });
      c.growth = sec("city.growth", () => {
        const g = city.Growth;
        const o = {
          turns_to_growth: g.turnsUntilGrowth,
          food: r1(g.currentFood),
          food_needed: r1(g.getNextGrowthFoodThreshold().value),
        };
        if (city.isTown && g.growthType != GrowthTypes.EXPAND) {
          o.town_focus = loc(GameInfo.Projects.lookup(g.projectType)?.Name);
        }
        if (city.isTown) o.food_sent_per_city = r1(city.getSentFoodPerCity());
        return o;
      });
      c.happiness = sec("city.happy", () => ({
        per_turn: r1(city.Happiness.netHappinessPerTurn),
        unrest: city.Happiness.hasUnrest,
      }));
      // build-queue/model-build-queue.js
      c.queue = sec("city.queue", () =>
        city.BuildQueue.getQueue().map((q) => {
          let name = String(q.orderType), type = null;
          if (q.orderType == OrderTypes.ORDER_CONSTRUCT) {
            const d = GameInfo.Constructibles.lookup(q.constructibleType);
            if (d) (name = loc(d.Name)), (type = d.ConstructibleType);
          } else if (q.orderType == OrderTypes.ORDER_TRAIN || q.orderType == OrderTypes.ORDER_FOOD_TRAIN) {
            const d = GameInfo.Units.lookup(q.unitType);
            if (d) (name = loc(d.Name)), (type = d.UnitType);
          } else if (q.orderType == OrderTypes.ORDER_ADVANCE) {
            const d = GameInfo.Projects.lookup(q.projectType);
            if (d) (name = loc(d.Name)), (type = d.ProjectType);
          }
          return {
            name, type,
            turns: city.BuildQueue.getTurnsLeft(q.type),
            pct: r1(city.BuildQueue.getPercentComplete(q.type)),
          };
        })
      );
      // city-details/model-city-details.js constructible loop
      c.constructibles = sec("city.cons", () => {
        const b = [], w = [], imp = {};
        for (const cid of city.Constructibles.getIds()) {
          const con = Constructibles.getByComponentID(cid);
          if (!con) continue;
          const d = GameInfo.Constructibles.lookup(con.type);
          if (!d) continue;
          const nm = loc(d.Name) + (con.damaged ? " (damaged)" : "");
          if (d.ConstructibleClass == "BUILDING") b.push(nm);
          else if (d.ConstructibleClass == "WONDER") w.push(nm);
          else if (d.ConstructibleClass == "IMPROVEMENT") imp[nm] = (imp[nm] || 0) + 1;
        }
        return { buildings: b, wonders: w, improvements: imp };
      });
      c.maintenance = sec("city.maint", () => {
        // model-yields-report.js: building maintenance
        const m = {};
        for (const cid of city.Constructibles.getIds()) {
          const con = Constructibles.getByComponentID(cid);
          if (!con) continue;
          const ms = city.Constructibles.getMaintenance(con.type);
          for (const i in ms) if (ms[i]) m[GameInfo.Yields[i].YieldType] = r1((m[GameInfo.Yields[i].YieldType] || 0) - ms[i]);
        }
        return m;
      });
      c.trade_route_count = sec("city.routes", () => city.Trade.routes.length); // screen-city-trade.js
      // city-details breakdown nodes: income from trade (always, cheap)
      c.trade_income = sec("city.tradeinc", () => {
        const o = {};
        for (let yi = 0; yi < YIELDS.length; yi++)
          for (const n of ["YIELD_FROM_TRADE", "YIELD_FROM_DEMAND_FOR_TRADE"]) {
            const v = city.Yields.getYieldsForNode(yi, [CityYieldNodes.INCOME, CityYieldNodes[n]], true);
            if (v && v.value) o[YIELDS[yi]] = r1((o[YIELDS[yi]] || 0) + v.value);
          }
        return o;
      });
      c.in_trade_network = sec("city.tnet", () => city.Trade.isInTradeNetwork());
      // commerce-screen-model.js
      c.resources = sec("city.res", () => ({
        assigned: city.Resources.getAssignedResources().map((r) => {
          const d = r.isOffMap
            ? GameInfo.Resources.lookup(local.Resources.getOffMapUniqueResourceFromId(r.value).uniqueResource.resource)
            : GameInfo.Resources.lookup(r.uniqueResource.resource);
          return loc(d?.Name);
        }),
        cap: city.Resources.getAssignedResourcesCap(),
      }));
      // city-details treasure fleet text
      if (Game.age == Game.getHash("AGE_EXPLORATION") && city.isDistantLands) {
        c.treasure_fleet = sec("city.treasure", () => {
          const r = city.Resources.canStartTreasureFleet();
          return r.Success
            ? { turns_until_next: city.Resources.getTurnsUntilTreasureGenerated() }
            : { blocked: (r.FailureReasons || []).map(loc) };
        });
      }
      if (OPT.detail) c.yield_breakdown = sec("city.breakdown", () => cityBreakdown(city.Yields));
      return c;
    })
  );

  // ---------------------------------------------------------------- trade (commerce screen)
  out.trade = sec("trade", () => {
    const tot = {};
    for (const c of out.settlements || []) for (const [k, v] of Object.entries(c.trade_income || {})) tot[k] = r1((tot[k] || 0) + v);
    const statusName = {};
    for (const k in TradeRouteStatus) statusName[TradeRouteStatus[k]] = k;
    const routes = { established: [], available: [], unavailable: [] };
    const opts = TradeRouteSearchOptions.INCLUDE_FAILED + TradeRouteSearchOptions.EXTENDED_STATUS;
    (local.Trade.projectPossibleTradeRoutes(opts) || []).forEach((route) => {
      const st = route.status || [];
      // The commerce screen hides NO_RESOURCES routes (commerce-screen-model.js); we keep them as
      // unavailable so a destination that stops offering resources doesn't silently vanish.
      const target = Cities.get(route.targetCityId), home = Cities.get(route.nearestCityId);
      if (!target || !home) return;
      const row = {
        to: loc(target.name),
        to_owner: target.owner,
        from: loc(home.name),
        domain: route.domain === DomainType.DOMAIN_LAND ? "land" : "sea",
        imports: (route.importPayloads || []).map((r) => loc(GameInfo.Resources.lookup(r.uniqueResource.resource)?.Name)),
        // Trade income paid to the destination's owner, not to us (pedia: "the foreign Settlement gains increased Trade Income").
        destination_income: (route.exportYields || []).map((y) => [GameInfo.Yields.lookup(y.yieldType)?.YieldType, r1(y.amount)]),
      };
      if (st.includes(TradeRouteStatus.ALREADY_EXISTS)) routes.established.push(row);
      else if (st.includes(TradeRouteStatus.SUCCESS)) routes.available.push(row);
      else routes.unavailable.push(Object.assign(row, { why: st.map((x) => statusName[x] || x) }));
    });
    return { trade_income_total: tot, routes };
  });

  // ---------------------------------------------------------------- other players (diplo ribbon)
  out.players = sec("players", () =>
    Players.getAlive()
      .filter((p) => p.isMajor && p.id != localID)
      .map((p) => {
        const met = hasMet(p.id);
        const o = { id: p.id, met };
        if (!met && !OPT.omniscient) return o;
        o.leader = loc(GameInfo.Leaders.lookup(p.leaderType)?.Name);
        o.civilization = loc(p.civilizationFullName);
        o.is_human = p.isHuman;
        const pd = p.Diplomacy;
        Object.assign(o, sec("p.rel", () => ({
          relationship: loc(pd.getRelationshipLevelName(localID)),
          relationship_level: pd.getRelationshipLevel(localID),
          at_war: localDiplo.isAtWarWith(p.id),
          allied: localDiplo.hasAllied(p.id),
        })) || {});
        // model-diplo-ribbon.js: yields shown on ribbon, trade routes, war support
        o.per_turn = sec("p.yields", () => netYields(p.Stats));
        o.settlements = sec("p.settle", () => p.Stats.numSettlements);
        o.trade_routes = sec("p.trade", () => ({
          current: local.Trade.countPlayerTradeRoutesTo(p.id),
          capacity: local.Trade.getTradeCapacityFromPlayer(p.id),
        }));
        if (o.at_war) {
          o.war_support = sec("p.war", () => {
            let warID = -1;
            for (const ev of Game.Diplomacy.getJointEvents(localID, p.id, false))
              if (ev.actionTypeName == "DIPLOMACY_ACTION_DECLARE_WAR") warID = ev.uniqueID;
            if (warID == -1) return null;
            const h = Game.Diplomacy.getDiplomaticEventData(warID);
            const sup = Game.Diplomacy.getSupportingPlayersWithBonusEnvoys(h.uniqueID).length;
            const opp = Game.Diplomacy.getOpposingPlayersWithBonusEnvoys(h.uniqueID).length;
            return { we_declared: h.initialPlayer == localID, our_support: h.initialPlayer == localID ? sup - opp : opp - sup };
          });
        }
        if (OPT.omniscient) o.gold_balance = sec("p.gold", () => r1(p.Treasury.goldBalance));
        return o;
      })
  );

  // city-states / independents with suzerain
  out.city_states = sec("citystates", () =>
    Players.getAlive()
      .filter((p) => p.isMinor && hasMet(p.id))
      .map((p) => ({
        id: p.id,
        name: loc(p.name),
        suzerain: sec("cs.suz", () => { const z = p.Influence.getSuzerain(); return z >= 0 ? (hasMet(z) || OPT.omniscient ? loc(Players.get(z)?.name) : "unmet civ") : null; }),
        at_war: localDiplo.isAtWarWith(p.id),
      }))
  );

  // ---------------------------------------------------------------- triumphs (ui-next/screens/legacies/legacies-model.js)
  // v1.5 replaced legacy-path scores (LegacyPaths.getScore now returns 0) with Legacies/"Triumphs".
  out.triumphs = sec("triumphs", () => {
    const pl = local.Legacies;
    const res = [];
    GameInfo.Legacies.forEach((t) => {
      if (!pl.isValidLegacy(t.LegacyType)) return;
      const pr = pl.getProgress(t.LegacyType);
      const row = {
        type: t.LegacyType,
        name: loc(t.Name),
        major: !!t.MajorLegacy,
        done: pl.isTriggered(t.LegacyType) ?? false,
        requirement: loc(t.TriggerDescription),
        reward: loc(t.Description),
      };
      if (pr && pr.progress && pr.progress[0]) (row.current = pr.progress[0].current), (row.goal = pr.progress[0].total);
      if (t.FirstPlayerOnly) row.first_only = true;
      if (pr && pr.raceWinner != -1 && pr.raceWinner !== undefined)
        row.race_winner = pr.raceWinner == localID ? "you" : hasMet(pr.raceWinner) ? loc(Players.get(pr.raceWinner)?.name) : "unmet";
      res.push(row);
    });
    return res;
  });
  // age-scores model (domination/score victories)
  out.victories = sec("victory", () => {
    const vm = Game.VictoryManager;
    const prog = vm.getVictoryProgress();
    const players = vm.getVictoryEnabledPlayers().map((id) => Players.get(id)).filter((p) => p && p.isMajor);
    return vm.getConfiguration().enabledVictories.map((v) => {
      const d = GameInfo.Victories.lookup(v);
      const rows = players.map((p) => {
        const pr = prog.find((x) => x.team == p.team && x.victory == v);
        return { player: hasMet(p.id) || OPT.omniscient ? loc(p.name) : "Unmet", id: p.id, score: pr ? Math.min(pr.current, pr.total) : 0, total: pr ? pr.total : 0 };
      });
      rows.sort((a, b) => b.score - a.score);
      return { victory: d?.VictoryType, name: loc(d?.Name), standings: rows };
    }).filter((v) => v.standings.some((r) => r.total > 1));
  });

  // countdown victories (GDP, Innovation, Tourism, Dominion): victories-screen-model.js / victories-screen.js
  // Verified live 2026-11-02 (Modern turn 3). On by default; pass victories:false to skip.
  if (OPT.victories !== false) out.countdown_victories = sec("countdown_victories", () =>
    ["VICTORY_ECONOMIC_MODERN", "VICTORY_SCIENCE_MODERN", "VICTORY_CULTURE_MODERN", "VICTORY_MILITARY_MODERN"]
      .filter((t) => GameInfo.Victories.lookup(t) && Game.VictoryManager.isCountdownVictoryEnabled(Database.makeHash(t)))
      .map((t) => {
        const victory = GameInfo.Victories.lookup(t);
        const rows = Players.getAlive()
          .filter((p) => p.Victories && p.isMajor)
          .map((p) => {
            const h = p.Victories.getHistoryForVictoryType(victory.$hash) || [];
            return {
              player: hasMet(p.id) || p.id == localID || OPT.omniscient ? loc(p.name) : "Unmet",
              id: p.id,
              points: p.Victories.getPointsForVictoryType(victory.$hash),
              last_turn_delta: h.length > 1 ? h[h.length - 1] - h[h.length - 2] : 0,
            };
          })
          .sort((a, b) => b.points - a.points);
        return { victory: t, target: Game.VictoryManager.getCountdownVictoryDominanceScore(victory.$hash), standings: rows };
      })
  );

  // ---------------------------------------------------------------- units
  const unitRow = (u) => {
    const d = GameInfo.Units.lookup(u.type);
    const o = { name: loc(u.name), type: d?.UnitType, class: d?.FormationClass, domain: d?.Domain, x: u.location.x, y: u.location.y };
    if (u.location.x < 0) (o.x = o.y = null), (o.in_army = true);
    if (u.Health) (o.hp = u.Health.maxDamage - u.Health.damage), (o.hp_max = u.Health.maxDamage);
    if (u.Movement) (o.moves = r1(u.Movement.movementMovesRemaining)), (o.moves_max = r1(u.Movement.maxMoves));
    if (u.isArmyCommander) o.commander = true;
    return o;
  };
  out.units = sec("units", () => local.Units.getUnits().map(unitRow));

  // visible enemy units (Visibility.isVisible precedent: unit flags)
  out.visible_enemy_units = sec("enemies", () => {
    const res = [];
    for (const p of Players.getAlive()) {
      if (p.id == localID || !localDiplo.isAtWarWith(p.id)) continue;
      for (const u of p.Units.getUnits()) {
        if (OPT.omniscient || Visibility.isVisible(localID, u.id)) res.push(Object.assign({ owner: p.id }, unitRow(u)));
      }
    }
    return res;
  });

  // foreign settlements the player has revealed on the map
  out.known_foreign_settlements = sec("foreign", () => {
    const res = [];
    for (const p of Players.getAlive()) {
      if (p.id == localID || !p.Cities) continue;
      for (const c of p.Cities.getCities()) {
        if (!OPT.omniscient && !revealed(c.location.x, c.location.y)) continue;
        res.push({ owner: p.id, name: loc(c.name), type: c.isTown ? "town" : "city", population: c.population, x: c.location.x, y: c.location.y });
      }
    }
    return res;
  });

  // ---------------------------------------------------------------- map (opt-in)
  // Calls mirror ui-next/tooltips/plot-tooltip/*.js and ui/mini-map (getGridWidth/Height).
  if (OPT.map) {
    out.map = sec("map", () => {
      const W = GameplayMap.getGridWidth(), H = GameplayMap.getGridHeight();
      const legend = { terrain: [], feature: [], resource: [], owner: {} };
      const code = (arr, v) => {
        if (v == null) return -1;
        let i = arr.indexOf(v);
        if (i < 0) (i = arr.length), arr.push(v);
        return i;
      };
      const plots = [];
      for (let y = 0; y < H; y++)
        for (let x = 0; x < W; x++) {
          const rs = GameplayMap.getRevealedState(localID, x, y);
          if (!OPT.omniscient && rs == RevealedStates.HIDDEN) continue;
          const owner = GameplayMap.getOwner(x, y);
          if (owner >= 0 && !(owner in legend.owner)) legend.owner[owner] = hasMet(owner) || OPT.omniscient ? loc(Players.get(owner)?.name) : "unmet";
          plots.push([
            x, y,
            rs == RevealedStates.VISIBLE ? 2 : 1,
            code(legend.terrain, typeOf("Terrains", GameplayMap.getTerrainType(x, y), "TerrainType")),
            code(legend.feature, typeOf("Features", GameplayMap.getFeatureType(x, y), "FeatureType")),
            code(legend.resource, typeOf("Resources", GameplayMap.getResourceType(x, y), "ResourceType")),
            owner,
            GameplayMap.getRiverType(x, y) != RiverTypes.NO_RIVER ? 1 : 0,
          ]);
        }
      return { width: W, height: H, columns: ["x", "y", "vis", "terrain", "feature", "resource", "owner", "river"], legend, plots };
    });
  }

  // ---------------------------------------------------------------- notifications
  out.notifications = sec("notifs", () => {
    const N = Game.Notifications;
    const rows = N.getIdsForPlayer(localID).map((id) => ({
      type: N.getTypeName(N.getType(id)),
      summary: loc(N.getSummary(id)),
      message: loc(N.getMessage(id)),
    }));
    return { end_turn_blocking: N.getEndTurnBlockingType(localID), items: rows };
  });

  out.errors = errors;
  return JSON.stringify(out);
})()
