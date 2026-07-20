import os
from datetime import datetime


# 環境変数で「現在時刻」を上書きする。未設定時は実時刻。
# 履歴リプレイ評価（過去日時の会話を投入して質問する）やテストで、
# システム時刻を会話の時系列に合わせて固定するための汎用フック。
# 本番では未設定なので datetime.now() のまま。
CHRONOS_NOW_ENV = "BUTLY_CHRONOS_NOW"


def resolve_now() -> datetime:
    override = os.environ.get(CHRONOS_NOW_ENV)
    if override:
        try:
            return datetime.fromisoformat(override)
        except ValueError:
            pass
    return datetime.now()


class ButlyChronos:
    def __init__(self, locale: str = "ja"):
        self.locale = locale

    def _get_time_segment(self, hour, is_weekday, is_holiday_override):
        """時間帯と状況によるモード判定"""
        # 1. 深夜・早朝
        if 0 <= hour < 6:
            return "Midnight Mode (深夜)", "深夜帯、夜更け"
        if 6 <= hour < 9:
            return "Morning Mode (早朝)", "早朝、夜明け"

        # 2. 日中 (09:00 - 18:00)
        if 9 <= hour < 18:
            # 平日 かつ 休み設定OFF の場合のみ仕事
            if is_weekday and not is_holiday_override:
                return "Business Mode (業務)", "仕事中、仕事の休憩"
            else:
                # 土日、または休暇モードON
                return "Holiday Daytime (休日日中)", "休みの日中"

        # 3. 夜
        if 18 <= hour < 24:
            return "Private Mode (夜)", "夕方から夜"

        return "Normal Mode", "通常"

    def get_delta_text(self, last_time, locale=None):
        """前回からの経過時間"""
        selected_locale = locale or self.locale
        if not last_time:
            return "First interaction" if selected_locale != "ja" else "初対面"
        now = resolve_now()
        delta = now - last_time
        seconds = delta.total_seconds()

        if selected_locale != "ja":
            if seconds < 60:
                value = int(seconds)
                unit = "second" if value == 1 else "seconds"
            elif seconds < 3600:
                value = int(seconds // 60)
                unit = "minute" if value == 1 else "minutes"
            elif seconds < 86400:
                value = int(seconds // 3600)
                unit = "hour" if value == 1 else "hours"
            else:
                value = int(seconds // 86400)
                unit = "day" if value == 1 else "days"
            return f"{value} {unit} since the last interaction"

        if seconds < 60:
            return f"{int(seconds)}秒ぶり"
        if seconds < 3600:
            return f"{int(seconds // 60)}分ぶり"
        if seconds < 86400:
            return f"{int(seconds // 3600)}時間ぶり"
        return f"{int(seconds // 86400)}日ぶり"

    def get_system_note(
        self,
        is_holiday=False,
        is_work_time=True,
        last_interaction_time=None,
        locale=None,
    ):
        """Web UI用の統合メソッド"""
        now = resolve_now()
        selected_locale = locale or self.locale
        weekday_map = (
            ["月", "火", "水", "木", "金", "土", "日"]
            if selected_locale == "ja"
            else ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        )

        return f"""
{now.strftime('%Y-%m-%d %H:%M')} ({weekday_map[now.weekday()]})

""".strip()
