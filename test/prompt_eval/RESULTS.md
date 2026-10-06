# Summarizer prompt eval — 20261001T055429Z

`2.1.286 (Claude Code)` · model `sonnet` · 3 run(s) per case and prompt · pass = every run passes

| case | v3 | v4 |
|---|---|---|
| en_ambiguous | ✅ 3/3 | ✅ 3/3 |
| en_deferred_aside | ❌ 1/3 | ✅ 3/3 |
| en_quotes_template | ✅ 3/3 | ✅ 3/3 |
| en_rejected_alternative | ❌ 2/3 | ✅ 3/3 |
| en_standing_rule | ✅ 3/3 | ✅ 3/3 |
| es_agreed_task | ❌ 0/3 | ✅ 3/3 |
| es_no_content | ✅ 3/3 | ✅ 3/3 |
| es_one_off_fact | ❌ 0/3 | ✅ 3/3 |
| es_open_question | ❌ 0/3 | ✅ 3/3 |
| es_reversal | ❌ 0/3 | ✅ 3/3 |

**Verdict:** v3 FAIL, v4 PASS

## Failures

- `en_deferred_aside` · v3 · run 1: not English (D15): ['del', 'el', 'en', 'es', 'la', 'lo', 'para', 'por', 'que', 'se', 'sin', 'un']
- `en_deferred_aside` · v3 · run 2: not English (D15): ['del', 'el', 'en', 'es', 'la', 'que', 'se', 'sin', 'un']
- `en_rejected_alternative` · v3 · run 2: not English (D15): ['del', 'el', 'en', 'la', 'los', 'para', 'por', 'que', 'se', 'una']
- `es_agreed_task` · v3 · run 1: not English (D15): ['del', 'en', 'es', 'la', 'lo', 'los', 'para', 'por', 'que', 'se', 'son', 'un', 'una']
- `es_agreed_task` · v3 · run 2: not English (D15): ['con', 'del', 'el', 'en', 'la', 'lo', 'los', 'más', 'para', 'por', 'se', 'un', 'una']
- `es_agreed_task` · v3 · run 3: not English (D15): ['del', 'en', 'es', 'la', 'lo', 'los', 'más', 'para', 'por', 'que', 'se', 'son', 'un', 'una']
- `es_one_off_fact` · v3 · run 1: not English (D15): ['con', 'del', 'el', 'en', 'es', 'está', 'lo', 'por', 'que', 'se']
- `es_one_off_fact` · v3 · run 2: not English (D15): ['con', 'del', 'el', 'en', 'es', 'está', 'lo', 'para', 'por', 'que', 'se']
- `es_one_off_fact` · v3 · run 3: not English (D15): ['con', 'del', 'el', 'en', 'es', 'está', 'la', 'lo', 'para', 'por', 'que', 'se']
- `es_open_question` · v3 · run 1: not English (D15): ['con', 'el', 'la', 'lo', 'los', 'más', 'para', 'que', 'se', 'sin']
- `es_open_question` · v3 · run 2: not English (D15): ['con', 'el', 'hay', 'la', 'lo', 'los', 'más', 'para', 'que', 'se', 'un']
- `es_open_question` · v3 · run 3: not English (D15): ['con', 'el', 'es', 'la', 'lo', 'los', 'más', 'para', 'que', 'se', 'sin']
- `es_reversal` · v3 · run 1: not English (D15): ['con', 'el', 'en', 'la', 'los', 'para', 'por', 'que', 'se', 'sin', 'una']
- `es_reversal` · v3 · run 2: not English (D15): ['con', 'del', 'el', 'en', 'la', 'los', 'para', 'por', 'se', 'una']
- `es_reversal` · v3 · run 3: not English (D15): ['con', 'del', 'el', 'en', 'está', 'la', 'los', 'para', 'pero', 'se', 'sin']
