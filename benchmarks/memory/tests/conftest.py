import json
import pytest

# Two-question miniature of the LongMemEval-S shape. Field names mirror the
# real dataset; verify_dataset enforces them at download time too.
FIXTURE_ROWS = [
    {
        "question_id": "q_multi_1",
        "question_type": "multi-session",
        "question": "What city did I move to?",
        "answer": "Lisbon",
        "question_date": "2023/05/20 (Sat) 10:00",
        "haystack_dates": ["2023/04/01 (Sat) 09:00", "2023/04/10 (Mon) 09:00"],
        "haystack_session_ids": ["s_a", "s_b"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I'm planning a move to Lisbon."},
                {"role": "assistant", "content": "Exciting! When do you move?"},
                {"role": "user", "content": "Next month."},
                {"role": "assistant", "content": "Good luck with the move."},
            ],
            [
                {"role": "user", "content": "What's a good pasta recipe?"},
                {"role": "assistant", "content": "Try cacio e pepe."},
            ],
        ],
        "answer_session_ids": ["s_a"],
    },
    {
        "question_id": "q_skip_1_abs",
        "question_type": "single-session-user",
        "question": "What is my dog's name?",
        "answer": "N/A (abstention)",
        "question_date": "2023/05/21 (Sun) 10:00",
        "haystack_dates": ["2023/04/02 (Sun) 09:00"],
        "haystack_session_ids": ["s_c"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I like hiking."},
                {"role": "assistant", "content": "Hiking is great exercise."},
            ]
        ],
        "answer_session_ids": [],
    },
]


@pytest.fixture
def fixture_dataset(tmp_path):
    p = tmp_path / "longmemeval_s.json"
    p.write_text(json.dumps(FIXTURE_ROWS), encoding="utf-8")
    return p
