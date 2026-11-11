# SafeRoute AI model: independent evaluation

Run 2026-11-11 on the laptop, read-only on the saved models and data. No other thread's process was touched. Real data only; the only
outside source is Safetipin's published audit method (see criteria.md). All numbers below were computed by the scripts in `scripts/`.

**Plain statement:** the "hand judgments" are Claude's judgment from the same aggregate numbers Qwen and the ranker see. They are not
people's answers and not ground truth. 0 people have answered any pair. Nothing here shows a route is actually safe.

## Ratings

| Component | Rating | Why |
|---|---|---|
| Lit head (is this street lit) | **Not usable** | Locked test (479 streets, 7 areas): error 18.6% (CI 9.8-31.0%) vs 17.8% for just guessing "lit" and 18.8% for a road-type table. AUROC 0.65. Trained on 6,347 labelled streets of 1.34M (0.47%), in 67 of 558 areas. |
| Open-places head (open_share) | **Modest, real** | My own refit on the same split: error 0.302 vs 0.354 for an hour-of-day average (better in 25 of 27 held-out areas). Biggest gain at night. Still a rough guess, and it predicts "are places open", not safety. |
| Bus head, traffic head | **Nothing to rate** | Trained on 0 rows. |
| Route ranker | **Decent copy of a careful reader, unproven on people** | ~92% agreement with Qwen on unseen areas, ~92% with my hand calls, but it has odd weights and no human check (below). |
| Qwen A/B labeller | **Fair on the pairs it commits to** | 90% agreement with my hand calls on pairs it answered; it gives no A/B answer on 42% of pairs (32% flip-flopped when A/B were swapped, 11% said "same"). |
| Simulated walker / agent | **No evidence** | Hazards are assumed numbers (their own reports say so). Held-out world walker arrived 0-30% of trips with 2x the events of the fastest route. |
| App's rule-based route score | **Disagrees with everything here** | Agrees with my hand calls 47% (n=53), with Qwen 50%. Needs a human check before anyone trusts either side. |

## 1. Lit head
- Only 0.47% of streets carry an OSM lit tag, so every one of the 1.08M predictions in `lit_predictions.csv.gz` is an extrapolation.
- Labelled streets are not like the rest. The most common street type is 61% of unlabelled streets but 19% of labelled ones; another type is 3% unlabelled vs 27% labelled. The model learned from the mapped minority.
- The labelled sample is also mostly lit (82% in the locked test), so "always lit" nearly ties it. Confident predictions (0.8+) cover 90% of streets with 15.6% error.
- Locked test is 7 areas, so even the error bar is wide.
- Top features (lanes, unpaved, building density, road type) are proxies for "main road", not lamps. `lamps_per_100m`, `n_cctv`, `n_lamp` had no measurable effect.
- Ranker input `lit_pct` is zero or missing for 68% of route sides. The ranker's biggest feature is mostly unavailable.

## 2. Open-places head (my refit, since the saved model also saw the locked areas)
Train 100,845 sampled rows from 126 areas; test 62,832 rows from 27 locked areas, same lock rule as `train.py`.
- Error 0.302 vs 0.354 (hour-of-day mean); Spearman 0.54 vs 0.45. Matches the saved report's 0.309 vs 0.348.
- Gain by hour: 0.08-0.09 from 00:00-06:00, 0.02-0.03 around midday and 19:00.
- Target is nearly binary: 29% exactly 0, 66% exactly 1. It is the share of nearby places **that have published hours** that are open, so a street with one 24-hour pharmacy scores 1 at 2 am. At 2 am the true mean is 0.43, which probably reflects the few places that publish hours (chains, fuel, pharmacies), not real late-night activity. Treat it as "known-hours places", not "people around".
- Bus and traffic: `traffic.csv` exists on disk but no rows reached the training table.

## 3. Route ranker and Qwen against my judgments
Setup: 80 pairs drawn with a fixed seed: 60 from the pairs Qwen answered, 20 from pairs Qwen could not settle. I judged each from the measures without seeing Qwen's or the ranker's answer, using `criteria.md`, which was written first. File: `hand_judgments.csv`. My verdicts: A 30, B 31, same 19.

| Judge | vs my calls, 60 Qwen-answered (decisive n=49) | 20 Qwen-failed (n=12) |
|---|---|---|
| Qwen | 89.8% (CI 79.6-98.0) | no answer |
| Ranker, refit without these pairs | 91.8% (CI 83.7-98.0) | 75% |
| Always the shorter route | 67.3% (CI 53.1-79.6) | 50% |

- Qwen vs ranker on all 484 Qwen labels, held-out by area: 91.9%. The saved model scores 94.6% on them but was trained on 299, so that figure is partly in-sample.
- Shorter-route baseline vs Qwen: 68.0%. Qwen matches "more lit" 95% when lit differs, and "fewer isolated metres" 83%.
- The comparison is not independent. Qwen, the ranker and I all read the same 38 aggregate numbers. Agreement shows consistent reading of the numbers, not that the numbers capture safety. The 60-pair sample also leans easy (Qwen only commits when its two orderings agree).
- My own fixed rubric agrees with my hand calls 100% (n=48), as it should; it is not a separate check. It agrees with Qwen 93.4% across 365 pairs.
- Calibration vs Qwen (held-out by area): confidence 0.5-0.6: 48% right (n=21); 0.6-0.7: 71% (n=21); 0.7-0.8: 89% (n=18); 0.8-0.9: 80% (n=45); 0.9+: 97% (n=379). Scores are very large (see problem 3), so probabilities saturate.

### Problems in the ranker
1. **Wrong direction at night.** Net night weights: `open_now` -0.36 per SD (an extra 5 open places lowers the night score), `shop_found` -0.39, `other_found` -0.18. All three should help.
2. **Longer is safer in daylight.** Length weight is +0.47 per SD by day: a 10 km route beats a 2 km one with everything else equal (+3.3). At night it flips hard negative (-41.8). The day sign is an artefact of Qwen's labels.
3. **Absurd magnitudes.** Police 100 m vs 3,000 m is worth +15 by day and +32 at night; lit 80% vs 0% is +31 to +43. Single measures dwarf everything else.
4. **Unknown is punished as unsafe.** `no_light_data_pct` costs -3.8 per SD, and lighting data is missing for most routes, so the ranker partly rewards "an OSM lit tag exists", i.e. well-mapped roads.
5. **Fragile.** Perturbing measures by 10-20% flips 11% of calls on average; 34% of pairs have a >10% chance of flipping.
6. Labels came from a 4B model that gave no A/B answer on 42% of pairs, so the ranker only learned from the easy ones.

## 4. App's rule-based score vs the pairs
I re-ran the app's route scorer on 76 of the 80 hand pairs (the saved measures reproduced exactly for 99% of routes).
- App vs my calls: 47% (n=53). Day 42%, night 52%. App vs Qwen: 50% (n=50). App vs "shorter": 56%.
- App's score gap tracks isolated metres (rho -0.50), but also tracks open places in the wrong direction (rho -0.45) and police distance (rho -0.30), and barely tracks lit % (-0.07) or main-road % (-0.11). Spearman against my rubric gap: 0.13.
- Caveats: n is small, the scorer also uses street-level inputs that are not in the pair table, and my judgments only see the aggregates. This is not proof the app is wrong, but it shows nothing here can say it is right.

## 4b. Fix: route ranker v2 (`rank_v2.py`, saved to `ml/out/route_ranker_v2.json`; the old `route_ranker.json` is untouched)
Same Qwen labels. Changes: every weight's sign is fixed by the criteria (more lit/open/CCTV/main road helps; isolation, distance to
police and extra length hurt), each measure is capped and scaled 0-1 so none can dominate, unknown lighting is neutral (no
`no_light_data_pct` term), and day and night get separate weights.

| | old ranker | v2 |
|---|---|---|
| Agreement with Qwen, held-out areas | 91.9% (n=484) | 89.9% (n=563, more labels now) |
| Agreement with my hand calls (trained without them) | 88.5% (n=61) | 98.4% (n=61) |
| Wrong-direction weights at night | 3 | 0 |
| Longer route judged safer by day | yes (+3.3 for 10 km vs 2 km) | no (-4.9) |
| Largest single-measure swing | +31.6 (police) | +5.0 (lit) |
| Calls flipped by 10-20% input noise | 11.1% | 6.9% |

Caveat: the sign rules come from the same criteria I judged with, so the 98.4% is partly built in. The fair gain is the sanity and
robustness rows. Still trained on Qwen, still unchecked against people.

Not fixable in code: the lit head needs more real lit labels (it has 0.47% coverage); keep it out of route scoring. The open-places
head should also report how many places with published hours each row rests on (`open_known`) before anyone reads it as activity.

## 5. What would change these ratings
1. Get 100+ real answers from people on `rate_pairs.py` (the one missing check). Compare people with Qwen, with the ranker, with the app score.
2. Drop the lit head from route scoring until it beats the majority guess; use only measured OSM tags and mark unknown as unknown.
3. Constrain the ranker: monotone signs per criteria, larger regularisation, no `length_m` day term, and treat missing lighting as neutral.
4. Rebuild `open_share` with an explicit "place has published hours" count and report it alongside.
5. Check why the app score falls as open places rise.

Files: `criteria.md`, `hand_judgments.csv`, `app_scores.json`, `common.py` (rubric), `scripts/` (every analysis run).
