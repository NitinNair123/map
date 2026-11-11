# Judging criteria (written BEFORE any agreement with Qwen or the ranker was computed)

This is Claude's judgment, not people's. It stands in for the human check that has not happened (0 people answered).
Limit: I see only the same aggregate numbers Qwen and the ranker see (pairs.jsonl), not the streets.

## Source
Safetipin's public audit method scores places on nine parameters, 0 (poor) to 3 (good): lighting, walk path, visibility
("eyes on the street"), openness (line of sight), public transport, security, people, gender usage, feeling. In its Delhi
report, lighting had the largest effect on perceived safety, then gender usage (presence of women) and visibility.
Reports: safetipin.com/wp-content/uploads/2020/01/Delhi-Report-Print.pdf ; Hyderabad-Safety-Audit-Report_Safetipin_2020.pdf.
Also used: Jacobs' "eyes on the street" (active frontage / open shops), standard CPTED ideas (isolation, escape/help access).

## Mapping from pair measures to those parameters
| Safetipin parameter | pair measures | direction |
|---|---|---|
| Lighting (largest) | lit_pct, dark_pct, no_light_data_pct | lit good, unlit bad, unknown mildly bad |
| People / visibility / gender usage | open_now, food/shop/other_found, main_pct, nothing_open_m | more open places and main roads good; long nothing-open stretches bad |
| Isolation / openness | isolated_m, help_gap_m | long stretches bad |
| Security | cctv, police_m, hospital/pharmacy/fuel found | more/closer good |
| Public transport | transit_found | more good |
| Exposure | length_m | longer walk = more exposure; matters more after dark |
| Data quality | unrated_pct | high = less certain, mild penalty |

## Fixed rubric (route score S; higher = safer). dark = daylight < 0.5
w = 1.5 after dark, else 1.0 for activity/isolation terms; lighting terms x1.5 after dark, x0.3 in daylight.

S =  L * (2.0*lit/100 - 2.0*dark/100 - 0.5*nolightdata/100)
   + w * ( 0.8*min(open_now,10)/10 + 0.3*min(food+shop+other,15)/15 + 0.5*main/100
           - 1.0*min(isolated,1500)/1500 - 0.7*min(nothing_open,1500)/1500 - 0.7*min(help_gap,1500)/1500 )
   + 0.3*min(cctv,5)/5 - 0.3*min(police_m,3000)/3000
   + 0.15*min(pharmacy+hospital+fuel,3)/3 + 0.2*min(transit,3)/3
   - 0.3*unrated/100
   - (0.8 if dark else 0.3) * (length - shorter)/shorter    [relative extra length, capped at 1.0]

Verdict for a pair: A if S_A - S_B > 0.15, B if < -0.15, otherwise "same".
These numbers are my own priors ordered by the Safetipin ranking, not fitted to anything, and were not tuned afterwards.

## Hand judgments
A separate blind sample (seeded, shown without Qwen's or the ranker's answer) is judged pair by pair by reading the
measures against the same criteria; hand verdicts are in hand_judgments.csv.

## Known blind spots (cannot be judged from these numbers)
Actual harassment/crime, who is on the street, CCTV working or not, street width/openness, women's presence, local reputation,
whether "lit" tags are current. Lighting itself is partly model-guessed (AUROC 0.65), so lit_pct is weak evidence.
