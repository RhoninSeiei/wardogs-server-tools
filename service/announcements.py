"""人工校对的中英日公告模板；不使用外部翻译接口。"""

EN_RULES = [
    '[EN] Beginner farming server: no fighting outside the hot zone. Fighting inside the hot zone is unrestricted.',
    '[EN] High KD does not prove cheating. KD > 4: announcement. KD > 6 with at least 7 kills: kick. Zero deaths count as 1. All kicks permanently restrict rejoining.',
    '[EN] Report base camping or other violations with complete evidence screenshots to the server moderators. Have fun!',
]
JA_RULES = [
    '[JA] 初心者向けファーミングサーバーです。ホットゾーン外での戦闘は禁止です。エリア内の戦闘は制限しません。',
    '[JA] 高KDだけで不正とは判断しません。KDが4超で告知、6超かつ7キル以上で退出処理します。デス0は1として計算します。退出処理後は無期限で再参加できません。',
    '[JA] リスポーン地点への攻撃などの違反は、完全な証拠スクリーンショットを添えて管理者へ報告してください。楽しく遊びましょう！',
]
REASONS = {
    '管理员手动踢出': ('a manual administrator action', '管理者による手動の退出指示'),
    '不符合本服要求': ('not meeting server requirements', '当サーバーの参加条件に適合しない行為'),
    '曾被踢出，触发永久重入限制': ('a permanent rejoin restriction following a prior kick', '過去の退出処理に基づく無期限の再参加制限'),
    '本局违规踢出后重新加入': ('rejoining after a rule-violation kick in this match', 'この試合中のルール違反による退出処理後の再参加'),
    '黄圈外杀人': ('killing players outside the hot zone', 'ホットゾーン外でのキル'),
    '黄圈外打人': ('attacking players outside the hot zone', 'ホットゾーン外での攻撃'),
    '黄圈外交战': ('fighting outside the hot zone', 'ホットゾーン外での戦闘'),
    '黄圈外使用防空武器击杀': ('killing players with anti-air weapons outside the hot zone', 'ホットゾーン外で対空兵器を使用したキル'),
    '违反本服规则，收到截图举报': ('a reported server-rule violation with screenshot evidence', '証拠画像付きで報告されたサーバールール違反'),
}


def validate_messages(messages):
    if any(not isinstance(s, str) or not s.strip() or len(s) > 200 or
           any(not c.isprintable() for c in s) for s in messages):
        raise ValueError('每种语言的公告须为1至200个可打印字符')
    return messages


def kick_messages(name, reason, english_reason=None, japanese_reason=None):
    if reason == '发送广告':
        return advertising_messages(name)
    known = REASONS.get(reason)
    en = english_reason or (known[0] if known else None)
    ja = japanese_reason or (known[1] if known else None)
    if not en or not ja:
        raise ValueError('未知原因须提供英语和日语译文')
    # Keep identity unchanged in each language. Detailed rules are sent periodically.
    return validate_messages([
        f'管理通告：玩家{name}因{reason}已被踢出。本服为社区服务器，黄圈外不打架，黄圈内不限制。',
        f'[EN] {name} was kicked for {en}. No fighting outside the hot zone.',
        f'[JA] {name}を{ja}により退出処理しました。ホットゾーン外での戦闘は禁止です。',
    ])


def kd_messages(name, kills, deaths, ratio, kicked=False):
    zh_stats = f'{name}：击杀{kills}、死亡{deaths}，KD={ratio:.2f}。'
    en_stats = f'{name}: kills={kills}, deaths={deaths}, KD={ratio:.2f}.'
    ja_stats = f'{name}：{kills}キル、{deaths}デス、KD={ratio:.2f}。'
    if kicked:
        return validate_messages([
            '管理通告：本服为社区服务器，已踢出' + zh_stats + '高KD不等于作弊，但不符合本服KD要求，予以踢出。',
            '[EN] Kicked: ' + en_stats + ' High KD does not prove cheating, but exceeds this beginner server limit (KD > 6, 7+ kills).',
            '[JA] 退出処理：' + ja_stats + '高KDだけで不正とは判断しませんが、初心者サーバーの上限（KD6超かつ7キル以上）を超えています。',
        ])
    return validate_messages([
        '数据提示：' + zh_stats + '零死亡按1计。高KD不等于作弊；违规请携完整证据截图到管理员举报。',
        '[EN] KD notice: ' + en_stats + ' Zero deaths count as 1. High KD does not prove cheating. Report violations with screenshots to the server moderators.',
        '[JA] KD告知：' + ja_stats + 'デス0は1として計算します。高KDだけで不正とは判断しません。違反は証拠画像付きで管理者へ報告してください。',
    ])


def offline_report_messages(identity):
    return validate_messages([
        f'举报处理提醒：SteamID {identity}所涉举报已核验；处理前该玩家已离线，未执行踢出。请遵守本服规则。',
        f'[EN] Verified report for SteamID {identity}: the player was already offline before processing; no kick was performed. Please follow server rules.',
        f'[JA] SteamID {identity}への通報は確認済みですが、処理前にオフラインでした。退出処理は実行していません。サーバールールを守ってください。',
    ])


def offline_admin_messages(identity):
    return validate_messages([
        f'管理通告：管理员要求移除SteamID {identity}，但处理前该玩家已离线，本次未执行踢出。',
        f'[EN] An administrator requested removal of SteamID {identity}, but the player was already offline. No kick was performed.',
        f'[JA] 管理者がSteamID {identity}の退出を指示しましたが、処理前にオフラインでした。今回は退出処理を実行していません。',
    ])


def advertising_messages(name, kicked=True):
    if kicked:
        return validate_messages([
            f'管理通告：玩家{name}因发送广告已被踢出，并永久限制重入。本服禁止广告。',
            f'[EN] {name} was kicked for advertising and is permanently barred from rejoining. Advertising is prohibited.',
            f'[JA] {name}を広告投稿により退出処理し、再参加を無期限で禁止しました。広告投稿は禁止です。',
        ])
    return validate_messages([
        f'管理通告：玩家{name}因发送广告被加入永久移除名单；处理时已离线，本次未执行踢出。本服禁止广告。',
        f'[EN] {name} is on the permanent removal list for advertising. Already offline when processed; no kick was performed. Advertising is prohibited.',
        f'[JA] {name}を広告投稿により無期限の退出対象リストに登録しました。処理時はオフラインのため、今回は退出操作を行っていません。広告投稿は禁止です。',
    ])
