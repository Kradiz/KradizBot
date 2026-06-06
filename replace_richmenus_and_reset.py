import os
import shutil
from datetime import datetime

import create_richmenus as crm


def backup_env_file():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = crm.ENV_PATH.with_name(f".env.richmenu-backup-{timestamp}")

    if crm.ENV_PATH.exists():
        shutil.copy2(crm.ENV_PATH, backup_path)
        print(f"[BACKUP] {backup_path}")
    else:
        print(f"[BACKUP] skip, .env not found: {crm.ENV_PATH}")

    return backup_path


def set_default_student_register_menu(new_values):
    default_id = new_values.get("STUDENT_RICH_MENU_REGISTER_ID", "").strip()
    if not default_id:
        print("[DEFAULT] missing STUDENT_RICH_MENU_REGISTER_ID, skip")
        return None

    res = crm.requests.post(
        f"{crm.LINE_API_BASE}/user/all/richmenu/{default_id}",
        headers={"Authorization": f"Bearer {crm.LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=20,
    )
    print(f"[DEFAULT] {res.status_code} {res.text}")
    return res


def delete_old_richmenus(old_values, new_values):
    new_ids = {value for value in new_values.values() if value}

    for key, old_id in old_values.items():
        if old_id and old_id not in new_ids:
            print(f"[DELETE OLD] {key}={old_id}")
            crm.delete_rich_menu(old_id)


def reset_existing_users():
    # Import after .env update so app reads the new rich menu ids.
    import app

    students_updated = 0
    teachers_updated = 0

    for student in app.get_worksheet("students").get_all_records():
        user_id = str(student.get("student_line_user_id", "")).strip()
        if user_id:
            app.unlink_rich_menu_from_user(user_id)
            app.update_student_rich_menu(user_id)
            students_updated += 1

    for teacher in app.get_worksheet("teachers").get_all_records():
        user_id = str(
            teacher.get("teacher_line_user_id", "")
            or teacher.get("line_user_id", "")
            or teacher.get("user_id", "")
        ).strip()
        if user_id:
            app.unlink_rich_menu_from_user(user_id)
            app.update_teacher_rich_menu(user_id)
            teachers_updated += 1

    print(f"[RESET] students_updated={students_updated}")
    print(f"[RESET] teachers_updated={teachers_updated}")


def main():
    print("========== Replace rich menus and reset users ==========")

    backup_env_file()

    crm.validate_before_run()
    crm.validate_images()

    old_env = crm.read_env_file()
    old_values = {
        key: old_env.get(key, "").strip()
        for key in crm.RICHMENU_ENV_KEYS
    }

    # Create all new menus before deleting old ones.
    new_values = crm.create_all_richmenus()
    crm.update_env_file(new_values)

    for key, value in new_values.items():
        os.environ[key] = value

    # If RENDER_API_KEY and RENDER_SERVICE_ID are set, update Render too.
    crm.update_render_env_vars(new_values)

    set_default_student_register_menu(new_values)
    delete_old_richmenus(old_values, new_values)
    reset_existing_users()

    print("========== DONE ==========")
    crm.print_result(new_values)


if __name__ == "__main__":
    main()
