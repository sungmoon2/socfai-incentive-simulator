# 결과 파일

`dispatch/`와 `cy/`는 각각 formal seed 0~99의 선별 결과다.

| 파일 | 내용 |
|---|---|
| `config.json` | 실행 조건과 고정 seed |
| `summary.json` | 평균 공유가치·기여도와 실행 요약 |
| `validation_report.json` | seed·조합·기여도 보존 검증 |
| `descriptive_stats.json` | 공유조합별 기술통계 |
| `bootstrap_ci.json` | paired bootstrap 신뢰구간 |
| `coalition_results.csv` | seed별 8개 조합의 KPI |
| `aggregate_contributions.csv` | 참여자별 평균 기여도 |
| `rewards.csv` | 정식 실행의 가상점수 |

README와 비교 SVG의 평균은 `descriptive_stats.json`의 `NONE.J`와 전체공유 J에서 가져온다. 전체공유 키는 배차 `CTR.J`, CY `CYR.J`다. 공유가치는 `summary.json`의 배차 `full_value_minutes`, CY `mean_full_value_moves`이며, 점수는 `rewards.csv`의 `integer_reward_points`다.

[source-manifest.json](source-manifest.json)은 source/config와 보존 파일의 경로·크기·SHA-256 및 출처 commit을 기록한다. [reference-manifest.json](reference-manifest.json)은 모델·mode·seed·조합 수, scientific 기준 해시와 선별 결과파일 해시를 기록한다.

전체 `scientific.json`과 `scenarios.jsonl`은 별도 `release-assets` 묶음으로 제공한다. 저장소의 공개 도구로도 다시 생성할 수 있다. `runtime_ms`는 해당 실행에서 측정된 값이며 scientific 해시의 비교 대상에서는 제외된다.
