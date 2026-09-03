# JSON 스키마 정의 (JSON Schemas)

이 문서는 skill-creator에서 사용되는 JSON 스키마를 정의합니다.

---

## evals.json

스킬에 대한 평가 항목을 정의합니다. 스킬 디렉터리 내 `evals/evals.json`에 위치합니다.

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "사용자의 예제 프롬프트",
      "expected_output": "기대되는 결과 설명",
      "files": ["evals/files/sample1.pdf"],
      "expectations": [
        "출력에 X가 포함되어 있다",
        "스킬이 스크립트 Y를 사용했다"
      ]
    }
  ]
}
```

**필드 설명:**
- `skill_name`: 스킬의 프론트매터 이름과 일치하는 스킬 이름
- `evals[].id`: 고유 정수 식별자
- `evals[].prompt`: 실행할 작업 프롬프트
- `evals[].expected_output`: 성공에 대한 사람이 읽을 수 있는 설명
- `evals[].files`: 선택적 입력 파일 경로 목록 (스킬 루트 기준 상대 경로)
- `evals[].expectations`: 검증 가능한 단언문/기대치 목록

---

## history.json

개선 모드에서 버전 진행 상황을 추적합니다. 작업 공간 루트에 위치합니다.

```json
{
  "started_at": "2026-01-15T10:30:00Z",
  "skill_name": "pdf",
  "current_best": "v2",
  "iterations": [
    {
      "version": "v0",
      "parent": null,
      "expectation_pass_rate": 0.65,
      "grading_result": "baseline",
      "is_current_best": false
    },
    {
      "version": "v1",
      "parent": "v0",
      "expectation_pass_rate": 0.75,
      "grading_result": "won",
      "is_current_best": false
    },
    {
      "version": "v2",
      "parent": "v1",
      "expectation_pass_rate": 0.85,
      "grading_result": "won",
      "is_current_best": true
    }
  ]
}
```

**필드 설명:**
- `started_at`: 개선 작업이 시작된 ISO 타임스탬프
- `skill_name`: 개선 중인 스킬의 이름
- `current_best`: 현재 최고 성능 버전의 식별자
- `iterations[].version`: 버전 식별자 (v0, v1, ...)
- `iterations[].parent`: 파생된 부모 버전
- `iterations[].expectation_pass_rate`: 채점 결과 통과율
- `iterations[].grading_result`: "baseline", "won", "lost", 또는 "tie"
- `iterations[].is_current_best`: 현재 최고 버전인지 여부

---

## grading.json

채점 에이전트의 출력입니다. `<run-dir>/grading.json`에 위치합니다.

```json
{
  "expectations": [
    {
      "text": "출력에 '홍길동' 이름이 포함되어 있다",
      "passed": true,
      "evidence": "트랜스크립트 3단계에서 발견: '추출된 이름: 홍길동, 이순신'"
    },
    {
      "text": "스프레드시트 B10 셀에 SUM 수식이 있다",
      "passed": false,
      "evidence": "스프레드시트가 생성되지 않음. 텍스트 파일만 출력됨."
    }
  ],
  "summary": {
    "passed": 2,
    "failed": 1,
    "total": 3,
    "pass_rate": 0.67
  },
  "execution_metrics": {
    "tool_calls": {
      "Read": 5,
      "Write": 2,
      "Bash": 8
    },
    "total_tool_calls": 15,
    "total_steps": 6,
    "errors_encountered": 0,
    "output_chars": 12450,
    "transcript_chars": 3200
  },
  "timing": {
    "executor_duration_seconds": 165.0,
    "grader_duration_seconds": 26.0,
    "total_duration_seconds": 191.0
  },
  "claims": [
    {
      "claim": "양식에 12개의 입력 필드가 있다",
      "type": "factual",
      "verified": true,
      "evidence": "field_info.json에서 12개 필드 확인"
    }
  ],
  "user_notes_summary": {
    "uncertainties": ["2023년 데이터를 사용함, 최신이 아닐 수 있음"],
    "needs_review": [],
    "workarounds": ["입력 불가 필드에 대해 텍스트 오버레이로 폴백"]
  },
  "eval_feedback": {
    "suggestions": [
      {
        "assertion": "출력에 '홍길동' 이름이 포함되어 있다",
        "reason": "단순 언급만으로도 통과될 수 있으므로 정확성 검증 추가 권장"
      }
    ],
    "overall": "단언문들이 존재 여부만 검사하고 정확성을 검사하지 않습니다."
  }
}
```

---

## metrics.json

실행 에이전트의 메트릭 출력입니다. `<run-dir>/outputs/metrics.json`에 위치합니다.

```json
{
  "tool_calls": {
    "Read": 5,
    "Write": 2,
    "Bash": 8,
    "Edit": 1,
    "Glob": 2,
    "Grep": 0
  },
  "total_tool_calls": 18,
  "total_steps": 6,
  "files_created": ["filled_form.pdf", "field_values.json"],
  "errors_encountered": 0,
  "output_chars": 12450,
  "transcript_chars": 3200
}
```

---

## timing.json

실행의 벽시계(Wall clock) 시간 측정 결과입니다. `<run-dir>/timing.json`에 위치합니다.

**캡처 방법:** 서브에이전트 작업 완료 시 알림에 `total_tokens`와 `duration_ms`가 포함됩니다. 사후 복구가 불가능하므로 이를 즉시 저장해야 합니다.

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3,
  "executor_start": "2026-01-15T10:30:00Z",
  "executor_end": "2026-01-15T10:32:45Z",
  "executor_duration_seconds": 165.0,
  "grader_start": "2026-01-15T10:32:46Z",
  "grader_end": "2026-01-15T10:33:12Z",
  "grader_duration_seconds": 26.0
}
```

---

## benchmark.json

벤치마크 모드의 집계 출력입니다. `benchmarks/<timestamp>/benchmark.json`에 위치합니다.

```json
{
  "metadata": {
    "skill_name": "pdf",
    "skill_path": "/path/to/pdf",
    "executor_model": "claude-sonnet-4-20250514",
    "analyzer_model": "most-capable-model",
    "timestamp": "2026-01-15T10:30:00Z",
    "evals_run": [1, 2, 3],
    "runs_per_configuration": 3
  },

  "runs": [
    {
      "eval_id": 1,
      "eval_name": "Ocean",
      "configuration": "with_skill",
      "run_number": 1,
      "result": {
        "pass_rate": 0.85,
        "passed": 6,
        "failed": 1,
        "total": 7,
        "time_seconds": 42.5,
        "tokens": 3800,
        "tool_calls": 18,
        "errors": 0
      },
      "expectations": [
        {"text": "...", "passed": true, "evidence": "..."}
      ],
      "notes": [
        "2023년 데이터를 사용함",
        "입력 불가 필드에 대해 텍스트 오버레이로 폴백"
      ]
    }
  ],

  "run_summary": {
    "with_skill": {
      "pass_rate": {"mean": 0.85, "stddev": 0.05, "min": 0.80, "max": 0.90},
      "time_seconds": {"mean": 45.0, "stddev": 12.0, "min": 32.0, "max": 58.0},
      "tokens": {"mean": 3800, "stddev": 400, "min": 3200, "max": 4100}
    },
    "without_skill": {
      "pass_rate": {"mean": 0.35, "stddev": 0.08, "min": 0.28, "max": 0.45},
      "time_seconds": {"mean": 32.0, "stddev": 8.0, "min": 24.0, "max": 42.0},
      "tokens": {"mean": 2100, "stddev": 300, "min": 1800, "max": 2500}
    },
    "delta": {
      "pass_rate": "+0.50",
      "time_seconds": "+13.0",
      "tokens": "+1700"
    }
  },

  "notes": [
    "'출력이 PDF 파일이다' 단언문은 두 구성 모두에서 100% 통과함 - 스킬 가치 변별력 낮음",
    "평가 3은 높은 분산(50% ± 40%)을 보임 - 불안정할 가능성 있음",
    "스킬 미사용 실행은 테이블 추출 기대치에서 일관되게 실패함",
    "스킬 사용 시 평균 13초가 더 소요되지만 통과율이 50% 향상됨"
  ]
}
```

**주의:** 뷰어는 이 필드 이름을 정확하게 읽습니다. `configuration` 대신 `config`를 사용하거나, `pass_rate`를 `result` 하위가 아닌 최상위에 두면 뷰어에 빈 값이 표시됩니다. 수동 생성 시 이 스키마를 엄격히 준수하십시오.

---

## comparison.json

블라인드 비교자의 출력입니다. `<grading-dir>/comparison-N.json`에 위치합니다.

```json
{
  "winner": "A",
  "reasoning": "출력 A는 올바른 서식과 모든 필수 필드를 갖춘 완전한 솔루션을 제공합니다. 출력 B는 날짜 필드가 누락되었고 서식이 일관되지 않습니다.",
  "rubric": {
    "A": {
      "content": {
        "correctness": 5,
        "completeness": 5,
        "accuracy": 4
      },
      "structure": {
        "organization": 4,
        "formatting": 5,
        "usability": 4
      },
      "content_score": 4.7,
      "structure_score": 4.3,
      "overall_score": 9.0
    },
    "B": {
      "content": {
        "correctness": 3,
        "completeness": 2,
        "accuracy": 3
      },
      "structure": {
        "organization": 3,
        "formatting": 2,
        "usability": 3
      },
      "content_score": 2.7,
      "structure_score": 2.7,
      "overall_score": 5.4
    }
  },
  "output_quality": {
    "A": {
      "score": 9,
      "strengths": ["완전한 솔루션", "우수한 서식", "모든 필드 존재"],
      "weaknesses": ["헤더의 경미한 스타일 불일치"]
    },
    "B": {
      "score": 5,
      "strengths": ["가독성 있는 출력", "올바른 기본 구조"],
      "weaknesses": ["날짜 필드 누락", "서식 불일치"]
    }
  },
  "expectation_results": {
    "A": {
      "passed": 4,
      "total": 5,
      "pass_rate": 0.80,
      "details": [
        {"text": "출력에 이름 포함", "passed": true}
      ]
    },
    "B": {
      "passed": 3,
      "total": 5,
      "pass_rate": 0.60,
      "details": [
        {"text": "출력에 이름 포함", "passed": true}
      ]
    }
  }
}
```

---

## analysis.json

사후 분석가의 출력입니다. `<grading-dir>/analysis.json`에 위치합니다.

```json
{
  "comparison_summary": {
    "winner": "A",
    "winner_skill": "path/to/winner/skill",
    "loser_skill": "path/to/loser/skill",
    "comparator_reasoning": "비교자가 승자를 선택한 이유 요약"
  },
  "winner_strengths": [
    "다중 페이지 문서 처리를 위한 명확한 단계별 지침",
    "서식 오류를 잡아내는 유효성 검사 스크립트 포함"
  ],
  "loser_weaknesses": [
    "'문서를 적절히 처리하라'는 모호한 지침으로 일관성 없는 동작 유발",
    "유효성 검사 스크립트가 없어 에이전트가 임의로 처리하다 오류 발생"
  ],
  "instruction_following": {
    "winner": {
      "score": 9,
      "issues": ["경미: 선택적 로깅 단계 건너뜀"]
    },
    "loser": {
      "score": 6,
      "issues": [
        "스킬의 서식 템플릿 미사용",
        "3단계를 따르지 않고 자체적인 접근 방식 고안"
      ]
    }
  },
  "improvement_suggestions": [
    {
      "priority": "high",
      "category": "instructions",
      "suggestion": "'문서를 적절히 처리하라'를 명시적 단계로 대체",
      "expected_impact": "일관성 없는 동작을 유발한 모호성 해소"
    }
  ],
  "transcript_insights": {
    "winner_execution_pattern": "스킬 읽기 -> 5단계 프로세스 준수 -> 유효성 검사 스크립트 사용",
    "loser_execution_pattern": "스킬 읽기 -> 접근 방식 혼선 -> 3가지 다른 방법 시도"
  }
}
```
