/*! gc-engine.js - date-range / player filtering for the Game Changers dashboards.
 *
 * Recomputes every player-based number in data.json from the per-match rows, so a page can be
 * re-cut by period and by player without a server. The maths mirrors src/analyzer.py,
 * src/selector.py and src/dashboard_generator.py; tests/ compares the two on real data.
 *
 *   GCEngine.compute(raw, {from, to, ids})  -> data shaped like data.json, for that slice
 *   GCEngine.mountFilterBar({el, raw, storageKey, onChange})  -> the shared filter UI
 */
(function (global) {
  'use strict';

  var SMALL = 1e-9;
  var FORMATS = [6, 10, 15, 20];
  var ALL_FMTS = [0, 6, 10, 15, 20];
  var ROLE_ORDER = ['wicket_keeper', 'batsman', 'all_rounder', 'bowler', 'unknown'];
  var FLEX = {
    bowler: ['bowler', 'all_rounder'],
    batsman: ['batsman', 'all_rounder'],
    all_rounder: ['all_rounder'],
    wicket_keeper: ['wicket_keeper'],
  };

  // ---------------------------------------------------------------- helpers

  /** Python's round(): half-to-even on the exact binary value (JS toFixed rounds exact ties up). */
  function pyRound(x, nd) {
    nd = nd || 0;
    if (typeof x !== 'number' || !isFinite(x)) return x;
    var neg = x < 0, ax = Math.abs(x);
    var full = ax.toFixed(Math.min(100, nd + 40));
    var dot = full.indexOf('.');
    var rest = full.slice(dot + 1 + nd);
    var out = Number(ax.toFixed(nd));
    if (/^50*$/.test(rest)) {
      var m = Math.pow(10, nd);
      var lower = Number(full.slice(0, dot + 1 + nd));
      var k = Math.round(lower * m);
      out = k % 2 === 0 ? lower : (k + 1) / m;
    }
    return neg ? -out : out;
  }

  function sum(a, f) { var s = 0; for (var i = 0; i < a.length; i++) s += f(a[i]); return s; }
  function mean(a) { return a.length ? sum(a, function (v) { return v; }) / a.length : 0; }
  function std1(a) {
    var n = a.length;
    if (n < 2) return 0;
    var m = mean(a), s = 0;
    for (var i = 0; i < n; i++) s += (a[i] - m) * (a[i] - m);
    return Math.sqrt(s / (n - 1));
  }
  function dayNum(iso) { return Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / 86400000; }
  function byRecency(a, b) {
    if (a.date !== b.date) return a.date < b.date ? 1 : -1;
    return Number(b.match_id) - Number(a.match_id);
  }

  function cfg(raw) {
    var c = raw.config || {};
    return {
      batAvg: c.bat_avg_benchmark, batSr: c.bat_sr_benchmark,
      econGood: c.bowl_econ_good, econBad: c.bowl_econ_bad,
      recentN: c.recent_matches_n,
      W: { recent: c.weight_recent_form, fmt: c.weight_format_specific,
           career: c.weight_career_avg, consist: c.weight_consistency },
      squad11: c.squad_11, squad15: c.squad_15,
    };
  }

  /** True when data.json carries the per-match fields the engine needs. */
  function supported(raw) {
    if (!raw || !raw.players || !raw.config || !raw.config.squad_11) return false;
    var p = raw.players.filter(function (x) { return (x.recent_matches || []).length; })[0];
    if (!p) return false;
    var r = p.recent_matches[0];
    return r.match_id !== undefined && r.batting_position !== undefined && r.catches !== undefined &&
      !!(raw.strategy && raw.strategy.batter_vs_bowler_rows);
  }

  // ------------------------------------------- per player / per format statistics

  function posStats(sub) {
    if (!sub.length) return { innings: 0, runs: 0, avg: 0, sr: 0 };
    var d = sum(sub, function (r) { return r.is_out; });
    var runs = sum(sub, function (r) { return r.runs; });
    var balls = sum(sub, function (r) { return r.balls_faced; });
    return { innings: sub.length, runs: runs,
             avg: pyRound(runs / Math.max(d, 1), 2), sr: pyRound(runs / Math.max(balls, 1) * 100, 2) };
  }

  function emptyStats() {
    var z = function () { return { innings: 0, runs: 0, avg: 0, sr: 0 }; };
    return {
      matches_played: 0, batting_avg: 0, batting_sr: 0, runs_total: 0, highest_score: 0,
      batting_std: 0, innings: 0, innings_batted: 0,
      batting_by_position: { top_order: z(), mid_order: z(), low_order: z() }, position_freq: {},
      bowling_avg: 0, bowling_economy: 0, bowling_sr: 0, overs_bowled_total: 0, wickets_total: 0,
      bowling_std: 0, dot_balls_total: 0, wides_total: 0, no_balls_total: 0, boundaries_conceded: 0,
      wides_per_over: 0, no_balls_per_over: 0, bdry_per_over: 0, dot_pct: 0, discipline_score: null,
      yet_to_bat_count: 0, did_not_bowl_count: 0, catches_total: 0, run_outs_total: 0,
      stumpings_total: 0, dismissals_taken: 0,
      recent_bat_avg: 0, recent_bat_sr: 0, recent_wkts: 0, recent_econ: 0,
    };
  }

  function formatStats(rows, fmt, C) {
    var p = fmt === 0 ? rows.slice() : rows.filter(function (r) { return r.overs === fmt; });
    if (!p.length) return emptyStats();
    p.sort(byRecency);

    var bat = p.filter(function (r) { return r.balls_faced > 0; });
    var innings = bat.length;
    var dismissals = sum(bat, function (r) { return r.is_out; });
    var runsTotal = sum(bat, function (r) { return r.runs; });
    var ballsTotal = sum(bat, function (r) { return r.balls_faced; });
    var highest = innings ? Math.max.apply(null, bat.map(function (r) { return r.runs; })) : 0;
    var battingAvg = pyRound(runsTotal / Math.max(dismissals, 1), 2);
    var battingSr = pyRound(runsTotal / Math.max(ballsTotal, 1) * 100, 2);
    var battingStd = innings > 1 ? std1(bat.map(function (r) { return r.runs; })) : 0;

    var bowl = p.filter(function (r) { return r.overs_bowled > 0; });
    var wkts = sum(bowl, function (r) { return r.wickets; });
    var conceded = sum(bowl, function (r) { return r.runs_conceded; });
    var overs = sum(bowl, function (r) { return r.overs_bowled; });
    var bowlingAvg = pyRound(conceded / Math.max(wkts, 1), 2);
    var bowlingEcon = pyRound(conceded / Math.max(overs, SMALL), 2);
    var bowlingStd = bowl.length > 1 ? std1(bowl.map(function (r) { return r.economy; })) : 0;

    var dots = sum(bowl, function (r) { return r.dot_balls; });
    var wides = sum(bowl, function (r) { return r.wides; });
    var noBalls = sum(bowl, function (r) { return r.no_balls; });
    var bdry = sum(bowl, function (r) { return r.boundaries_conceded; });
    var ballsBowled = overs > 0 ? pyRound(overs * 6, 0) : 0;
    var bowlingSr = wkts > 0 ? pyRound(ballsBowled / Math.max(wkts, 1), 2) : 0;
    var wpo = pyRound(wides / Math.max(overs, SMALL), 2);
    var nbpo = pyRound(noBalls / Math.max(overs, SMALL), 2);
    var bpo = pyRound(bdry / Math.max(overs, SMALL), 2);
    var dotPct = pyRound(dots / Math.max(ballsBowled, 1) * 100, 1);
    var discipline = overs > 0 ? Math.max(0, pyRound(100 - wpo * 8 - nbpo * 10 - bpo * 4, 1)) : null;

    var catches = sum(p, function (r) { return r.catches; });
    var runOuts = sum(p, function (r) { return r.run_outs; });
    var stumpings = sum(p, function (r) { return r.stumpings; });

    var inPos = function (lo, hi) {
      return bat.filter(function (r) { return r.batting_position != null && r.batting_position >= lo && r.batting_position <= hi; });
    };
    var freq = {};
    bat.forEach(function (r) { if (r.batting_position != null) freq[r.batting_position] = (freq[r.batting_position] || 0) + 1; });

    var recent = p.slice(0, C.recentN);
    var rb = recent.filter(function (r) { return r.balls_faced > 0; });
    var rw = recent.filter(function (r) { return r.overs_bowled > 0; });

    return {
      matches_played: p.length,
      batting_avg: battingAvg, batting_sr: battingSr, runs_total: runsTotal, highest_score: highest,
      batting_std: battingStd, innings: innings, innings_batted: innings,
      batting_by_position: {
        top_order: posStats(inPos(1, 3)), mid_order: posStats(inPos(4, 6)),
        low_order: posStats(bat.filter(function (r) { return r.batting_position != null && r.batting_position >= 7; })),
      },
      position_freq: freq,
      overs_bowled_total: pyRound(overs, 2), bowling_avg: bowlingAvg, bowling_economy: bowlingEcon,
      bowling_sr: bowlingSr, wickets_total: wkts, bowling_std: bowlingStd, dot_balls_total: dots,
      wides_total: wides, no_balls_total: noBalls, boundaries_conceded: bdry,
      wides_per_over: wpo, no_balls_per_over: nbpo, bdry_per_over: bpo, dot_pct: dotPct,
      discipline_score: discipline,
      yet_to_bat_count: sum(p, function (r) { return r.yet_to_bat; }),
      did_not_bowl_count: sum(p, function (r) { return r.did_not_bowl; }),
      catches_total: catches, run_outs_total: runOuts, stumpings_total: stumpings,
      dismissals_taken: catches + runOuts + stumpings,
      recent_bat_avg: pyRound(rb.length ? mean(rb.map(function (r) { return r.runs; })) : 0, 2),
      recent_bat_sr: pyRound(rb.length ? mean(rb.map(function (r) { return r.batting_sr; })) : 0, 2),
      recent_wkts: pyRound(rw.length ? mean(rw.map(function (r) { return r.wickets; })) : 0, 2),
      recent_econ: pyRound(rw.length ? mean(rw.map(function (r) { return r.economy; })) : 0, 2),
    };
  }

  function inferRole(s) {
    var hasBat = (s.innings || 0) > 0;
    var hasBowl = (s.wickets_total || 0) > 0 || (s.bowling_avg || 0) > 0;
    if (hasBat && hasBowl) return 'all_rounder';
    if (hasBowl) return 'bowler';
    if (hasBat) return 'batsman';
    return 'unknown';
  }

  function composite(s, role, C) {
    if (s.matches_played === 0) return 0;
    var W = C.W;
    var batAvgNorm = Math.min(s.batting_avg / C.batAvg, 1.0) * 100;
    var batSrNorm = Math.min(s.batting_sr / C.batSr, 1.0) * 100;
    var batRecentAvg = Math.min(s.recent_bat_avg / C.batAvg, 1.0) * 100;
    var batRecentSr = Math.min((s.recent_bat_sr || 0) / C.batSr, 1.0) * 100;
    var batRecent = batRecentAvg * 0.6 + batRecentSr * 0.4;
    var batConsist = Math.max(0, 100 - Math.min(s.batting_std / 15.0, 1.0) * 100);
    var battingScore = W.recent * batRecent + W.fmt * batSrNorm + W.career * batAvgNorm + W.consist * batConsist;

    var econRange = C.econBad - C.econGood;
    var econInv = s.bowling_economy > 0 ? Math.max(0, (C.econBad - s.bowling_economy) / econRange) * 100 : 0;
    var recentEconInv = s.recent_econ > 0 ? Math.max(0, (C.econBad - s.recent_econ) / econRange) * 100 : 0;
    var wktsNorm = Math.min(s.wickets_total / 15.0, 1.0) * 100;
    var bowlConsist = Math.max(0, 100 - Math.min(s.bowling_std / 3.0, 1.0) * 100);
    var bowlingScore = W.recent * recentEconInv + W.fmt * econInv + W.career * wktsNorm + W.consist * bowlConsist;

    var disc = s.discipline_score;
    if (disc !== null && disc !== undefined) bowlingScore = Math.max(0, bowlingScore + (disc - 50) / 10.0);

    var matches = Math.max(s.matches_played, 1);
    var fieldBonus = Math.min(s.dismissals_taken / matches * 10, 5.0);

    var score;
    if (role === 'batsman') score = battingScore + fieldBonus * 0.5;
    else if (role === 'bowler') score = bowlingScore + fieldBonus * 0.5;
    else if (role === 'wicket_keeper') {
      var keep = Math.min(s.stumpings_total * 2 + s.catches_total, 10);
      score = battingScore * 0.75 + keep + fieldBonus;
    } else if (role === 'all_rounder') score = battingScore * 0.55 + bowlingScore * 0.45 + fieldBonus;
    else score = battingScore * 0.5 + bowlingScore * 0.5 + fieldBonus * 0.5;
    return pyRound(Math.min(score, 100.0), 2);
  }

  function classifyForm(s) {
    if (s.batting_avg === 0) return 'Insufficient Data';
    var avgRatio = s.recent_bat_avg / s.batting_avg;
    var srRatio = s.batting_sr > 0 ? s.recent_bat_sr / s.batting_sr : 1.0;
    var combined = avgRatio * 0.6 + srRatio * 0.4;
    if (combined >= 1.2) return 'In Form';
    if (combined <= 0.6) return 'Out of Form';
    return 'Average';
  }

  function trajectory(s) {
    if (s.batting_avg === 0) return 'stable';
    var ratio = s.batting_avg > 0 ? s.recent_bat_avg / s.batting_avg : 1.0;
    if (ratio >= 1.25) return 'up';
    if (ratio <= 0.60) return 'down';
    return 'stable';
  }

  function weekendTrend(rows, C) {
    var none = { weekends: [], trend: 'insufficient_data', slope: 0.0, scores: [] };
    if (rows.length < 2) return none;
    var p = rows.slice().sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
    var clusters = [], cur = [p[0]];
    for (var i = 1; i < p.length; i++) {
      if (dayNum(p[i].date) - dayNum(cur[cur.length - 1].date) <= 2) cur.push(p[i]);
      else { clusters.push(cur); cur = [p[i]]; }
    }
    clusters.push(cur);
    if (clusters.length < 2) return none;

    var weekends = clusters.map(function (c) {
      var bat = c.filter(function (r) { return r.balls_faced > 0; });
      var bowl = c.filter(function (r) { return r.overs_bowled > 0; });
      var runs = sum(bat, function (r) { return r.runs; });
      var dism = sum(bat, function (r) { return r.is_out; });
      var wk = sum(bowl, function (r) { return r.wickets; });
      var rc = sum(bowl, function (r) { return r.runs_conceded; });
      var ov = sum(bowl, function (r) { return r.overs_bowled; });
      var batSc = Math.min((runs / Math.max(dism, 1)) / C.batAvg, 1.0) * 100;
      var econ = rc / Math.max(ov, 0.001);
      var bowlSc = ov > 0 ? Math.max(0, (C.econBad - econ) / (C.econBad - C.econGood)) * 100 : 0;
      var score = (runs > 0 && wk > 0) ? pyRound(batSc * 0.5 + bowlSc * 0.5, 1)
                : wk > 0 ? pyRound(bowlSc, 1) : pyRound(batSc, 1);
      return { dates: c.map(function (r) { return r.date; }).sort(), score: score, runs: runs, wickets: wk, matches: c.length };
    });

    var scores = weekends.map(function (w) { return w.score; });
    var n = scores.length, mx = (n - 1) / 2, my = mean(scores), sxy = 0, sxx = 0;
    for (var j = 0; j < n; j++) { sxy += (j - mx) * (scores[j] - my); sxx += (j - mx) * (j - mx); }
    var flat = Math.max.apply(null, scores) === Math.min.apply(null, scores);
    var slope = (n >= 2 && !flat) ? sxy / sxx : 0.0;
    var trend = slope > 3.0 ? 'growing' : slope < -3.0 ? 'declining' : 'stable';
    return { weekends: weekends, trend: trend, slope: pyRound(slope, 2), scores: scores };
  }

  function analysePlayer(p, rows, C) {
    var stats = {}, role = p.role || 'unknown';
    ALL_FMTS.forEach(function (fmt) {
      var s = formatStats(rows, fmt, C);
      if (role === 'unknown') { var inf = inferRole(s); if (inf !== 'unknown') role = inf; }
      s.form_score = composite(s, role, C);
      stats[fmt] = s;
    });
    return { player_id: p.id, name: p.name, role: role, formats: stats,
             overall_score: stats[0].form_score, wow_trend: weekendTrend(rows, C) };
  }

  // -------------------------------------------------------------- squad selection

  function roleIdx(role) {
    var r = role || 'unknown', i = ROLE_ORDER.indexOf(r);
    return i >= 0 ? i : ROLE_ORDER.length;
  }

  function selectSquad(analysis, fmt, size, C) {
    var constraints = size === 11 ? C.squad11 : C.squad15;
    var pool = analysis.map(function (p) {
      var fs = p.formats[fmt] || {}, all = p.formats[0] || {};
      var has = (fs.matches_played || 0) > 0;
      var chosen = has ? fs : all;
      var score = has ? chosen.form_score : pyRound((chosen.form_score || 0) * 0.80, 2);
      return {
        player_id: p.player_id, name: p.name, role: p.role, score: score, has_fmt_data: has,
        matches_played: chosen.matches_played || 0, batting_avg: chosen.batting_avg || 0,
        batting_sr: chosen.batting_sr || 0, bowling_economy: chosen.bowling_economy || 0,
        wickets_total: chosen.wickets_total || 0, runs_total: chosen.runs_total || 0,
        catches_total: chosen.catches_total || 0, dismissals_taken: chosen.dismissals_taken || 0,
      };
    });
    pool.sort(function (a, b) { return b.score - a.score; });

    var noData = pool.filter(function (p) { return p.matches_played === 0; });
    pool = pool.filter(function (p) { return p.matches_played > 0; });
    var selected = [], remaining = pool.slice(), warnings = [];
    var counts = {};
    ROLE_ORDER.forEach(function (r) { counts[r] = 0; });

    Object.keys(constraints).forEach(function (role) {
      var minN = constraints[role][0];
      var roles = FLEX[role] || [role];
      var eligible = remaining.filter(function (p) { return roles.indexOf(p.role) >= 0; });
      if (eligible.length < minN) {
        eligible = eligible.concat(remaining.filter(function (p) { return p.role === 'unknown'; }));
        warnings.push("Only " + eligible.length + " players available for role '" + role + "' (need " + minN + ")");
      }
      eligible.slice(0, minN).forEach(function (pick) {
        selected.push(Object.assign({}, pick, { reason: 'Required ' + role + ' (min ' + minN + ')' }));
        remaining = remaining.filter(function (p) { return p.player_id !== pick.player_id; });
        counts[role] += 1;
      });
    });

    var slotsLeft = size - selected.length;
    remaining.slice().forEach(function (player) {
      if (slotsLeft <= 0) return;
      var role = player.role;
      var maxN = constraints[role] ? constraints[role][1] : size;
      if ((counts[role] || 0) >= maxN) return;
      selected.push(Object.assign({}, player, { reason: 'Best available ' + role }));
      counts[role] = (counts[role] || 0) + 1;
      remaining = remaining.filter(function (p) { return p.player_id !== player.player_id; });
      slotsLeft -= 1;
    });

    slotsLeft = size - selected.length;
    if (slotsLeft > 0) {
      warnings.push('Short ' + slotsLeft + ' player(s) — filling without role constraints');
      remaining.slice(0, slotsLeft).forEach(function (player) {
        selected.push(Object.assign({}, player, { reason: 'Best available (role cap override)' }));
        remaining = remaining.filter(function (p) { return p.player_id !== player.player_id; });
      });
    }

    selected.sort(function (a, b) { return (roleIdx(a.role) - roleIdx(b.role)) || (b.score - a.score); });
    var benchWith = remaining.slice().sort(function (a, b) { return b.score - a.score; });
    var summary = {};
    ROLE_ORDER.forEach(function (r) { if (r !== 'unknown') summary[r] = counts[r] || 0; });
    return { squad: selected, bench: benchWith.concat(noData).slice(0, 5), warnings: warnings, role_summary: summary };
  }

  function selectAll(analysis, C) {
    var out = {};
    FORMATS.forEach(function (fmt) {
      out[String(fmt)] = { '11': selectSquad(analysis, fmt, 11, C), '15': selectSquad(analysis, fmt, 15, C) };
    });
    return out;
  }

  function whyNot(pid, selections) {
    var out = {};
    Object.keys(selections).forEach(function (fmt) {
      var r = selections[fmt]['11'];
      var inSquad = r.squad.filter(function (p) { return p.player_id === pid; })[0];
      var inBench = r.bench.filter(function (p) { return p.player_id === pid; })[0];
      if (inSquad) out[fmt] = 'Selected — ' + (inSquad.reason || '');
      else if (inBench) out[fmt] = 'On bench (score ' + pyRound(inBench.score || 0, 1).toFixed(1) + ')';
      else out[fmt] = 'Not selected — score below threshold';
    });
    return out;
  }

  // ------------------------------------------------- opportunity / accountability

  function buildOpportunity(players, analyses, rowsBy, selections, totalTeamMatches, raw) {
    var prev = raw.opportunity || {};
    var thresholdsByRole = prev.min_thresholds || {};
    var allRows = [];
    players.forEach(function (p) { allRows = allRows.concat(rowsBy.get(p.id)); });
    var teamBat = allRows.length ? mean(allRows.map(function (r) { return r.runs; })) : 0;
    var bowlRows = allRows.filter(function (r) { return r.overs_bowled > 0; });
    var teamEcon = bowlRows.length ? mean(bowlRows.map(function (r) { return r.economy; })) : (allRows.length ? null : 0);

    var out = players.map(function (p) {
      var a = analyses.get(p.id), rows = rowsBy.get(p.id), role = p.role || 'unknown';
      var formats = {};
      ALL_FMTS.forEach(function (fmt) {
        var fs = a.formats[fmt], m = fs.matches_played;
        formats[String(fmt)] = {
          discipline: {
            wides_per_over: fs.wides_per_over, no_balls_per_over: fs.no_balls_per_over,
            bdry_per_over: fs.bdry_per_over, dot_pct: fs.dot_pct, discipline_score: fs.discipline_score,
            wides_total: fs.wides_total, no_balls_total: fs.no_balls_total,
            boundaries_conceded: fs.boundaries_conceded,
          },
          batting_position: fs.batting_by_position,
          position_freq: fs.position_freq,
          chances: {
            yet_to_bat: fs.yet_to_bat_count, did_not_bowl: fs.did_not_bowl_count,
            batting_chance_pct: pyRound((m - fs.yet_to_bat_count) / Math.max(m, 1) * 100, 1),
            bowling_chance_pct: (role === 'bowler' || role === 'all_rounder')
              ? pyRound((m - fs.did_not_bowl_count) / Math.max(m, 1) * 100, 1) : null,
          },
          peer_comparison: {
            bat_avg_vs_team: pyRound(fs.batting_avg - teamBat, 2),
            econ_vs_team: (fs.bowling_economy > 0 && teamEcon !== null) ? pyRound(fs.bowling_economy - teamEcon, 2) : null,
            team_bat_avg: pyRound(teamBat, 2),
            team_bowl_econ: teamEcon === null ? null : pyRound(teamEcon, 2),
          },
        };
      });

      var seen = {};
      rows.forEach(function (r) { seen[r.match_id] = 1; });
      var oppIdx = (!allRows.length || totalTeamMatches === 0) ? 0.0
                 : pyRound(Object.keys(seen).length / totalTeamMatches * 100, 1);

      var thr = thresholdsByRole[role] || {};
      var s0 = a.formats[0], meets = {};
      if (Object.keys(thr).length) {
        meets.matches = s0.matches_played >= thr.matches;
        if ('batting_avg' in thr) meets.batting_avg = s0.batting_avg >= thr.batting_avg;
        if ('batting_sr' in thr) meets.batting_sr = s0.batting_sr >= thr.batting_sr;
        if ('bowling_economy' in thr) meets.bowling_economy = s0.bowling_economy > 0 && s0.bowling_economy <= thr.bowling_economy;
        var mm = Math.max(s0.matches_played, 1);
        if ('wickets_per_match' in thr) meets.wickets_per_match = s0.wickets_total / mm >= thr.wickets_per_match;
        if ('dismissals_per_match' in thr) meets.dismissals_per_match = s0.dismissals_taken / mm >= thr.dismissals_per_match;
      }
      return { id: p.id, name: p.name, role: role, formats: formats, why_not_selected: whyNot(p.id, selections),
               opportunity_index: oppIdx, trajectory: trajectory(s0), meets_minimum: meets, thresholds: thr };
    });
    return { players: out, min_thresholds: prev.min_thresholds || {}, benchmarks: prev.benchmarks || {} };
  }

  // -------------------------------------------------------------------- strategy

  function buildStrategy(raw, players, rowsBy, ids, inRange) {
    var S = raw.strategy || {};
    var matches = new Map(), econ = new Map();
    players.forEach(function (p) {
      rowsBy.get(p.id).forEach(function (r) {
        var m = matches.get(r.match_id);
        if (!m) {
          m = { match_id: r.match_id, match_date: r.date, opponent: r.opponent, result: r.result,
                match_overs: r.overs, match_runs: 0, match_wickets: 0 };
          matches.set(r.match_id, m); econ.set(r.match_id, []);
        }
        if (r.batting_position != null) m.match_runs += r.runs;
        m.match_wickets += r.wickets;
        if (r.overs_bowled > 0) econ.get(r.match_id).push(r.economy);
      });
    });
    var raws = Array.from(matches.values()).sort(function (a, b) {
      return a.match_date !== b.match_date ? (a.match_date < b.match_date ? 1 : -1) : Number(b.match_id) - Number(a.match_id);
    });

    var byOpp = new Map();
    raws.forEach(function (m) {
      var o = byOpp.get(m.opponent);
      if (!o) { o = { opponent: m.opponent, matches: 0, wins: 0, losses: 0, total_runs_scored: 0, total_wickets_taken: 0, _e: [] }; byOpp.set(m.opponent, o); }
      o.matches++; if (m.result === 'win') o.wins++; if (m.result === 'loss') o.losses++;
      o.total_runs_scored += m.match_runs; o.total_wickets_taken += m.match_wickets;
      var e = econ.get(m.match_id); if (e.length) o._e.push(mean(e));
    });
    var perOpponent = Array.from(byOpp.values()).map(function (o) {
      var e = o._e; delete o._e; o.avg_economy = e.length ? mean(e) : null; return o;
    }).sort(function (a, b) { return b.matches - a.matches; });

    var ours = (S.our_bowlers_dismissals || []).filter(function (b) { return ids.has(b.bowler_player_id); })
      .map(function (b) {
        var v = (b.victims || []).filter(function (x) { return inRange(x.date); });
        return Object.assign({}, b, { victims: v, total: v.length });
      }).filter(function (b) { return b.total > 0; }).sort(function (a, b) { return b.total - a.total; });

    var theirs = (S.opponent_bowlers_vs_us || []).map(function (b) {
      var d = (b.our_batters_dismissed || []).filter(function (x) { return ids.has(x.batter_player_id) && inRange(x.date); });
      return Object.assign({}, b, { our_batters_dismissed: d, total: d.length });
    }).filter(function (b) { return b.total > 0; }).sort(function (a, b) { return b.total - a.total; });

    var oppStats = (S.opponent_bowler_stats || []).map(function (b) {
      var pm = (b.per_match || []).filter(function (x) { return matches.has(x.match_id) && inRange(x.date); });
      var ov = sum(pm, function (x) { return x.overs; }), rc = sum(pm, function (x) { return x.runs; });
      return Object.assign({}, b, { per_match: pm, matches_vs_us: pm.length, total_overs: ov, total_runs: rc,
        total_wickets: sum(pm, function (x) { return x.wickets; }),
        avg_economy: ov > 0 ? pyRound(rc / Math.max(ov, 0.001), 2) : 0 });
    }).filter(function (b) { return b.matches_vs_us > 0; }).sort(function (a, b) { return b.total_wickets - a.total_wickets; });

    var nameById = new Map(players.map(function (p) { return [p.id, p.name]; }));
    var groups = new Map();
    (S.batter_vs_bowler_rows || []).forEach(function (r) {
      if (!ids.has(r.batter_player_id) || !inRange(r.date)) return;
      var g = groups.get(r.batter_player_id);
      if (!g) { g = { batter_name: nameById.get(r.batter_player_id), batter_player_id: r.batter_player_id, _b: new Map() }; groups.set(r.batter_player_id, g); }
      var v = g._b.get(r.bowler_name);
      if (!v) { v = { bowler_name: r.bowler_name, bowler_ch_id: r.bowler_ch_id, _m: new Set(), runs: 0, balls: 0, fours: 0, sixes: 0, dismissals: 0, dot_balls: 0 }; g._b.set(r.bowler_name, v); }
      v._m.add(r.match_id); v.runs += r.runs; v.balls += r.balls; v.fours += r.fours; v.sixes += r.sixes;
      v.dismissals += r.dismissals; v.dot_balls += r.dot_balls;
    });
    var bvb = Array.from(groups.values()).map(function (g) {
      var vs = Array.from(g._b.values()).map(function (v) {
        var o = { bowler_name: v.bowler_name, bowler_ch_id: v.bowler_ch_id, matches: v._m.size, runs: v.runs, balls: v.balls,
                  fours: v.fours, sixes: v.sixes, dismissals: v.dismissals, dot_balls: v.dot_balls,
                  strike_rate: v.balls > 0 ? pyRound(v.runs * 100.0 / v.balls, 1) : 0 };
        return o;
      }).sort(function (a, b) { return b.runs - a.runs; });
      return { batter_name: g.batter_name, batter_player_id: g.batter_player_id, vs_bowlers: vs };
    }).sort(function (a, b) { return a.batter_name < b.batter_name ? -1 : a.batter_name > b.batter_name ? 1 : 0; });

    return Object.assign({}, S, {
      per_opponent: perOpponent, per_opponent_raw: raws, our_bowlers_dismissals: ours,
      opponent_bowlers_vs_us: theirs, opponent_bowler_stats: oppStats, batter_vs_bowler: bvb,
      batter_vs_bowler_rows: [],   // consumed here; not needed downstream
    });
  }

  // ----------------------------------------------------------------- compute()

  /** filter = {from:'YYYY-MM-DD'|null, to:'YYYY-MM-DD'|null, ids:Set<playerId>|null (null = everyone)} */
  function compute(raw, filter) {
    var f = filter || {}, C = cfg(raw);
    var inRange = function (d) { return (!f.from || d >= f.from) && (!f.to || d <= f.to); };

    var rowsBy = new Map(), universe = new Set();
    raw.players.forEach(function (p) {
      var rows = (p.recent_matches || []).filter(function (r) { return r.is_game_changers_match == 1 && inRange(r.date); });
      rows.sort(byRecency);
      rowsBy.set(p.id, rows);
      rows.forEach(function (r) { universe.add(r.match_id); });
    });

    // With a period set, players who did not play in it are left out (rows of zeros are noise).
    // With no period the full squad stays, exactly as published.
    var periodSet = !!(f.from || f.to);
    var chosen = raw.players.filter(function (p) {
      return (!f.ids || f.ids.has(p.id)) && (!periodSet || rowsBy.get(p.id).length > 0);
    });
    var ids = new Set(chosen.map(function (p) { return p.id; }));

    var analyses = new Map();
    chosen.forEach(function (p) { analyses.set(p.id, analysePlayer(p, rowsBy.get(p.id), C)); });
    var ranked = chosen.map(function (p) { return analyses.get(p.id); });
    ranked.sort(function (a, b) { return b.overall_score - a.overall_score; });
    var selections = selectAll(ranked, C);

    var players = chosen.map(function (p) {
      var a = analyses.get(p.id), fmts = {};
      ALL_FMTS.forEach(function (fmt) {
        var s = a.formats[fmt];
        fmts[String(fmt)] = {
          matches_played: s.matches_played, innings_batted: s.innings_batted || 0,
          batting_avg: pyRound(s.batting_avg || 0, 2), batting_sr: pyRound(s.batting_sr || 0, 2),
          runs_total: s.runs_total || 0, highest_score: s.highest_score || 0,
          bowling_avg: pyRound(s.bowling_avg || 0, 2), bowling_economy: pyRound(s.bowling_economy || 0, 2),
          wickets_total: s.wickets_total || 0, overs_bowled_total: pyRound(s.overs_bowled_total || 0, 2),
          form_score: pyRound(s.form_score || 0, 2), recent_bat_avg: pyRound(s.recent_bat_avg || 0, 2),
        };
      });
      return Object.assign({}, p, {
        form_badge: classifyForm(a.formats[0]), overall_score: pyRound(a.overall_score || 0, 2),
        formats: fmts, wow_trend: a.wow_trend, recent_matches: rowsBy.get(p.id),
      });
    });

    var opportunity = buildOpportunity(chosen, analyses, rowsBy, selections, universe.size, raw);
    var strategy = buildStrategy(raw, chosen, rowsBy, ids, inRange);

    var dates = [], appearances = 0;
    chosen.forEach(function (p) { rowsBy.get(p.id).forEach(function (r) { dates.push(r.date); appearances++; }); });
    dates.sort();
    var summary = {
      first: dates[0] || null, last: dates[dates.length - 1] || null,
      days: dates.length ? Math.round(dayNum(dates[dates.length - 1]) - dayNum(dates[0])) + 1 : 0,
      teamMatches: universe.size, appearances: appearances,
      selectedPlayers: chosen.length, totalPlayers: raw.players.length,
    };

    return Object.assign({}, raw, {
      team: Object.assign({}, raw.team, { nct_matches: universe.size }),
      players: players, selections: selections, opportunity: opportunity, accountability: opportunity,
      strategy: strategy, summary: summary,
    });
  }

  // -------------------------------------------------------------- filter bar UI

  function h(tag, props) {
    var el = document.createElement(tag), p = props || {};
    Object.keys(p).forEach(function (k) {
      var v = p[k];
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) el.setAttribute(k, v === true ? '' : v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (Array.isArray(c)) c.forEach(function (x) { if (x != null) el.append(x); });
      else if (c != null) el.append(c);
    }
    return el;
  }

  var fmtDate = function (iso) {
    return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  };
  var addDays = function (iso, n) { return new Date((dayNum(iso) + n) * 86400000).toISOString().slice(0, 10); };
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var ROLE_LABEL = { batsman: 'Batsman', bowler: 'Bowler', all_rounder: 'All-rounder', wicket_keeper: 'Keeper' };

  /**
   * opts: {el, raw, storageKey, onChange(derivedRaw, state)}
   * Returns {setRaw(raw), reset(), state()}.
   */
  function mountFilterBar(opts) {
    var raw = opts.raw, root = opts.el, key = opts.storageKey;
    var bounds, monthsPresent, matchCount;
    var state = { from: null, to: null, preset: 'all', ids: null };
    var panelOpen = false, search = '';

    function recomputeBounds() {
      var dates = [];
      matchCount = new Map();
      raw.players.forEach(function (p) {
        (p.recent_matches || []).forEach(function (r) { if (r.is_game_changers_match == 1) dates.push(r.date); });
      });
      dates.sort();
      bounds = { min: dates[0] || null, max: dates[dates.length - 1] || null };
      var seen = {};
      dates.forEach(function (d) { seen[d.slice(0, 7)] = 1; });
      monthsPresent = Object.keys(seen).sort().reverse();
    }

    function load() {
      try {
        var s = JSON.parse(localStorage.getItem(key) || 'null');
        if (s && typeof s === 'object') {
          state.from = s.from || null; state.to = s.to || null; state.preset = s.preset || 'custom';
          if (Array.isArray(s.chs)) {
            var known = {};
            raw.players.forEach(function (p) { known[String(p.cricheroes_id)] = p.id; });
            var ids = s.chs.map(function (c) { return known[String(c)]; }).filter(function (x) { return x !== undefined; });
            state.ids = ids.length ? new Set(ids) : null;
          }
        }
      } catch (e) { /* storage unavailable: start with no filter */ }
      clamp();
    }

    function save() {
      try {
        var chs = state.ids ? raw.players.filter(function (p) { return state.ids.has(p.id); }).map(function (p) { return String(p.cricheroes_id); }) : null;
        localStorage.setItem(key, JSON.stringify({ from: state.from, to: state.to, preset: state.preset, chs: chs }));
      } catch (e) { /* ignore */ }
    }

    function clamp() {
      if (!bounds.min) { state.from = state.to = null; return; }
      if (state.from && state.from < bounds.min) state.from = bounds.min;
      if (state.to && state.to > bounds.max) state.to = bounds.max;
      if (state.from && state.to && state.from > state.to) state.to = state.from;
      if (state.from === bounds.min && state.to === bounds.max) { state.from = state.to = null; state.preset = 'all'; }
    }

    function applyPreset(v) {
      state.preset = v;
      if (v === 'all') { state.from = state.to = null; }
      else if (v.indexOf('month:') === 0) {
        var ym = v.slice(6), last = new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7), 0)).getUTCDate();
        state.from = ym + '-01'; state.to = ym + '-' + String(last).padStart(2, '0');
      } else {
        var n = parseInt(v, 10);
        state.to = bounds.max; state.from = addDays(bounds.max, -(n - 1));
      }
      clamp();
    }

    function filter() {
      return { from: state.from, to: state.to, ids: state.ids };
    }

    var out;
    function emit() {
      save();
      out = compute(raw, filter());
      render();
      if (opts.onChange) opts.onChange(out, state);
    }

    function isFiltered() { return !!(state.from || state.to || state.ids); }

    function inPeriodCounts() {
      var c = new Map();
      raw.players.forEach(function (p) {
        c.set(p.id, (p.recent_matches || []).filter(function (r) {
          return r.is_game_changers_match == 1 && (!state.from || r.date >= state.from) && (!state.to || r.date <= state.to);
        }).length);
      });
      return c;
    }

    function setIds(fn) {
      var cur = new Set(state.ids || raw.players.map(function (p) { return p.id; }));
      fn(cur);
      state.ids = cur.size === raw.players.length ? null : cur;
      emit();
    }

    var inputCls = 'bg-slate-800 border border-slate-600/60 text-gray-300 text-xs rounded-lg px-2.5 py-1.5 focus:outline-none focus:border-amber-500';
    var btnCls = 'text-xs px-3 py-1.5 rounded-lg border border-slate-600/60 bg-slate-800 text-gray-300 hover:border-amber-500 hover:text-amber-400 transition';

    function render() {
      var s = out.summary, filtered = isFiltered();
      var kids = [];

      kids.push(h('div', { class: 'flex flex-wrap items-center gap-x-3 gap-y-1' },
        h('span', { class: 'text-lg', text: '📅' }),
        h('span', { class: 'text-slate-500 text-xs uppercase tracking-widest', text: 'Data period' }),
        h('span', { class: 'text-amber-400 font-semibold text-sm',
                    text: s.first ? fmtDate(s.first) + ' → ' + fmtDate(s.last) : 'No matches in this selection' }),
        h('span', { class: 'text-xs text-slate-500',
                    text: s.first ? s.teamMatches + (s.teamMatches === 1 ? ' team match' : ' team matches') + ' · ' + s.days + (s.days === 1 ? ' day' : ' days') + ' · ' + s.appearances +
                          (s.appearances === 1 ? ' player appearance' : ' player appearances') + ' · games for other teams excluded' : '' }),
        filtered ? h('span', { class: 'text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full border bg-amber-500/10 text-amber-400 border-amber-500/30', text: 'Filtered' }) : null));

      var presetSel = h('select', { class: inputCls, 'aria-label': 'Quick range', onchange: function (e) {
        if (e.target.value !== 'custom') { applyPreset(e.target.value); emit(); }
      } });
      var presets = [['all', 'All matches'], ['30', 'Last 30 days'], ['60', 'Last 60 days'], ['90', 'Last 90 days'],
                     ['180', 'Last 6 months'], ['365', 'Last 12 months']];
      presets.forEach(function (o) { presetSel.append(h('option', { value: o[0], text: o[1], selected: state.preset === o[0] })); });
      monthsPresent.forEach(function (ym) {
        presetSel.append(h('option', { value: 'month:' + ym, text: MONTHS[+ym.slice(5, 7) - 1] + ' ' + ym.slice(0, 4),
                                       selected: state.preset === 'month:' + ym }));
      });
      presetSel.append(h('option', { value: 'custom', text: 'Custom range', selected: state.preset === 'custom', disabled: state.preset !== 'custom' }));

      var field = function (label, input) {
        return h('label', { class: 'flex flex-col gap-1 text-[10px] uppercase tracking-widest text-slate-500' }, label, input);
      };
      var dateInput = function (which, label) {
        return field(label, h('input', { type: 'date', class: inputCls, min: bounds.min, max: bounds.max,
          value: state[which] || (which === 'from' ? bounds.min : bounds.max), 'aria-label': label,
          onchange: function (e) {
            var v = e.target.value || null;
            if (!v) return;
            state[which] = v;
            if (!state.from) state.from = bounds.min;
            if (!state.to) state.to = bounds.max;
            if (state.from > state.to) { if (which === 'from') state.to = state.from; else state.from = state.to; }
            state.preset = 'custom';
            clamp(); emit();
          } }));
      };

      var nSel = state.ids ? state.ids.size : raw.players.length;
      var playersBtn = h('button', { type: 'button', class: btnCls + ' flex items-center gap-2', 'aria-expanded': String(panelOpen),
        'aria-haspopup': 'true', onclick: function (e) { e.stopPropagation(); panelOpen = !panelOpen; render(); if (panelOpen) { var i = root.querySelector('[data-gc-search]'); if (i) i.focus(); } } },
        h('span', { text: '👥' }),
        h('span', { text: 'Players: ' + nSel + ' of ' + raw.players.length }),
        h('span', { class: 'text-slate-500', text: panelOpen ? '▲' : '▼' }));

      kids.push(h('div', { class: 'mt-3 flex flex-wrap items-end gap-3 border-t border-slate-800 pt-3' },
        field('Quick range', presetSel), dateInput('from', 'From'), dateInput('to', 'To'),
        h('div', { class: 'sm:relative' }, field('Players', playersBtn), panelOpen ? playersPanel() : null),
        filtered ? h('button', { type: 'button', class: btnCls, text: 'Reset filters', onclick: function () { reset(); } }) : null));

      if (nSel === 0) kids.push(h('p', { class: 'mt-2 text-xs text-red-300', text: 'No players selected — pick at least one to see any stats.' }));
      root.replaceChildren.apply(root, kids);
    }

    function playersPanel() {
      var counts = inPeriodCounts();
      var q = search.trim().toLowerCase();
      var list = raw.players.slice().sort(function (a, b) { return a.name.localeCompare(b.name); })
        .filter(function (p) { return !q || p.name.toLowerCase().indexOf(q) >= 0; });
      var sel = function (p) { return !state.ids || state.ids.has(p.id); };

      var rows = list.map(function (p) {
        var n = counts.get(p.id) || 0;
        return h('label', { class: 'flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-slate-700/30 cursor-pointer text-sm' },
          h('input', { type: 'checkbox', class: 'accent-amber-500', checked: sel(p),
            onchange: function (e) { setIds(function (cur) { if (e.target.checked) cur.add(p.id); else cur.delete(p.id); }); } }),
          h('span', { class: 'flex-1 truncate ' + (n ? 'text-white' : 'text-slate-500'), title: p.name, text: p.name }),
          h('span', { class: 'text-[10px] text-slate-500', text: ROLE_LABEL[p.role] || '' }),
          h('span', { class: 'text-xs tabular-nums w-8 text-right ' + (n ? 'text-amber-400' : 'text-slate-600'), title: 'Matches in the selected period', text: String(n) }));
      });

      var quick = function (label, fn) { return h('button', { type: 'button', class: 'text-xs px-2 py-1 rounded-md border border-slate-600/60 bg-slate-800 text-gray-300 hover:border-amber-500', text: label, onclick: fn }); };
      var roleBtn = function (role) {
        return quick(ROLE_LABEL[role], function () {
          setIds(function (cur) { cur.clear(); raw.players.forEach(function (p) { if (p.role === role) cur.add(p.id); }); });
        });
      };

      return h('div', { class: 'absolute left-3 right-3 sm:left-0 sm:right-auto z-50 mt-2 sm:w-[22rem] rounded-xl border border-slate-600/60 bg-slate-900 shadow-2xl p-3',
        role: 'dialog', 'aria-label': 'Choose players', onclick: function (e) { e.stopPropagation(); } },
        h('input', { type: 'search', 'data-gc-search': '', class: inputCls + ' w-full mb-2', placeholder: 'Search player…', value: search,
          oninput: function (e) { search = e.target.value; var pos = e.target.selectionStart; render(); var i = root.querySelector('[data-gc-search]'); if (i) { i.focus(); i.setSelectionRange(pos, pos); } } }),
        h('div', { class: 'flex flex-wrap gap-1.5 mb-2' },
          quick('All', function () { state.ids = null; emit(); }),
          quick('None', function () { state.ids = new Set(); emit(); }),
          quick('Played in period', function () { setIds(function (cur) { cur.clear(); counts.forEach(function (n, id) { if (n) cur.add(id); }); }); }),
          ['batsman', 'bowler', 'all_rounder', 'wicket_keeper'].map(roleBtn)),
        h('div', { class: 'max-h-64 overflow-y-auto pr-1' }, rows.length ? rows : h('p', { class: 'text-xs text-slate-500 p-2', text: 'No player matches your search.' })),
        h('p', { class: 'mt-2 text-[10px] text-slate-500', text: 'The number on the right is matches played in the chosen period.' }));
    }

    function reset() {
      state = { from: null, to: null, preset: 'all', ids: null };
      search = ''; panelOpen = false;
      emit();
    }

    document.addEventListener('click', function () { if (panelOpen) { panelOpen = false; render(); } });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && panelOpen) { panelOpen = false; render(); } });

    recomputeBounds();
    load();
    emit();

    return {
      setRaw: function (r) { raw = r; recomputeBounds(); clamp(); emit(); },
      reset: reset,
      state: function () { return state; },
      current: function () { return out; },
    };
  }

  global.GCEngine = { compute: compute, supported: supported, mountFilterBar: mountFilterBar, pyRound: pyRound };
})(typeof window !== 'undefined' ? window : globalThis);
