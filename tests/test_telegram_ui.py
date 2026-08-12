from opportunity_sentinel.telegram_bot import (
    MAJORS,
    main_menu,
    major_keyboard,
    type_keyboard,
    user_id_from_thread,
    year_keyboard,
)


def test_all_student_navigation_is_button_driven() -> None:
    assert all(button.callback_data for row in main_menu().inline_keyboard for button in row)
    assert len(major_keyboard().inline_keyboard) == len(MAJORS)
    assert all(
        button.callback_data
        for row in type_keyboard("onboard").inline_keyboard
        for button in row
    )
    assert all(button.callback_data for row in year_keyboard().inline_keyboard for button in row)


def test_workflow_thread_is_bound_to_authenticated_telegram_user() -> None:
    assert user_id_from_thread("opp-12345-a1b2c3") == 12345
    assert user_id_from_thread("alert-67890-a1b2c3") == 67890
    assert user_id_from_thread("unknown-12345-a1b2c3") is None
    assert user_id_from_thread("opp-not-a-number-a1b2c3") is None
