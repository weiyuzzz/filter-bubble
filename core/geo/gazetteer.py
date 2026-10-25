"""地名库：城市与地区（离线、可审计）。

* 国家名不在这里，而是由 ``countries.py`` 从 Natural Earth 生成的数据提供，
  避免手工维护 200 多个国家名；
* 这里只维护**新闻里高频出现的城市/地区**，每条都能说出坐标依据（官方首都/城市中心）；
* 匹配按"最长优先 + 不重叠"，并对公司名/组织名等会造成误判的后缀做排除。

条目格式 ``中文|英文|经度|纬度|ISO2|类型``，类型为 ``city`` 或 ``region``。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

CITY = "city"
REGION = "region"

_RAW = """
北京|beijing|116.4074|39.9042|CN|city
上海|shanghai|121.4737|31.2304|CN|city
广州|guangzhou|113.2644|23.1291|CN|city
深圳|shenzhen|114.0579|22.5431|CN|city
天津|tianjin|117.2010|39.0842|CN|city
重庆|chongqing|106.5516|29.5630|CN|city
成都|chengdu|104.0665|30.5723|CN|city
杭州|hangzhou|120.1551|30.2741|CN|city
武汉|wuhan|114.3055|30.5928|CN|city
西安|xian|108.9402|34.3416|CN|city
南京|nanjing|118.7969|32.0603|CN|city
沈阳|shenyang|123.4315|41.8057|CN|city
哈尔滨|harbin|126.5349|45.8038|CN|city
长春|changchun|125.3235|43.8171|CN|city
大连|dalian|121.6147|38.9140|CN|city
青岛|qingdao|120.3826|36.0671|CN|city
济南|jinan|117.1205|36.6510|CN|city
郑州|zhengzhou|113.6254|34.7466|CN|city
合肥|hefei|117.2272|31.8206|CN|city
福州|fuzhou|119.2965|26.0745|CN|city
厦门|xiamen|118.0894|24.4798|CN|city
南昌|nanchang|115.8582|28.6820|CN|city
长沙|changsha|112.9388|28.2282|CN|city
南宁|nanning|108.3665|22.8170|CN|city
昆明|kunming|102.8329|24.8801|CN|city
贵阳|guiyang|106.6302|26.6477|CN|city
太原|taiyuan|112.5489|37.8706|CN|city
石家庄|shijiazhuang|114.5149|38.0428|CN|city
兰州|lanzhou|103.8343|36.0611|CN|city
西宁|xining|101.7782|36.6171|CN|city
银川|yinchuan|106.2309|38.4872|CN|city
乌鲁木齐|urumqi|87.6168|43.8256|CN|city
拉萨|lasa|91.1409|29.6456|CN|city
呼和浩特|hohhot|111.7519|40.8414|CN|city
苏州|suzhou|120.6199|31.2990|CN|city
无锡|wuxi|120.3019|31.5747|CN|city
东莞|dongguan|113.7518|23.0207|CN|city
佛山|foshan|113.1214|23.0215|CN|city
珠海|zhuhai|113.5763|22.2707|CN|city
汕头|shantou|116.6820|23.3541|CN|city
温州|wenzhou|120.6994|27.9944|CN|city
宁波|ningbo|121.5447|29.8683|CN|city
洛阳|luoyang|112.4540|34.6197|CN|city
唐山|tangshan|118.1802|39.6304|CN|city
烟台|yantai|121.4479|37.4638|CN|city
澳门|macau|113.5439|22.1987|MO|city
香港|hong kong|114.1694|22.3193|HK|city
台北|taipei|121.5654|25.0330|TW|city
高雄|kaohsiung|120.3014|22.6273|TW|city
台南|tainan|120.2270|22.9998|TW|city
新竹|hsinchu|120.9675|24.8138|TW|city
新疆|xinjiang|87.6168|43.8256|CN|region
西藏|tibet|91.1409|29.6456|CN|region
内蒙古|inner mongolia|111.7519|40.8414|CN|region
广西|guangxi|108.3665|22.8170|CN|region
宁夏|ningxia|106.2309|38.4872|CN|region
青海|qinghai|101.7782|36.6171|CN|region
甘肃|gansu|103.8343|36.0611|CN|region
陕西|shaanxi|108.9402|34.3416|CN|region
山西|shanxi|112.5489|37.8706|CN|region
河北|hebei|114.5149|38.0428|CN|region
河南|henan|113.6254|34.7466|CN|region
山东|shandong|117.1205|36.6510|CN|region
江苏|jiangsu|118.7969|32.0603|CN|region
浙江|zhejiang|120.1551|30.2741|CN|region
安徽|anhui|117.2272|31.8206|CN|region
福建|fujian|119.2965|26.0745|CN|region
江西|jiangxi|115.8582|28.6820|CN|region
湖北|hubei|114.3055|30.5928|CN|region
湖南|hunan|112.9388|28.2282|CN|region
广东|guangdong|113.2644|23.1291|CN|region
海南|hainan|110.3316|20.0311|CN|region
四川|sichuan|104.0665|30.5723|CN|region
贵州|guizhou|106.6302|26.6477|CN|region
云南|yunnan|102.8329|24.8801|CN|region
辽宁|liaoning|123.4315|41.8057|CN|region
吉林|jilin|125.3235|43.8171|CN|region
黑龙江|heilongjiang|126.5349|45.8038|CN|region
东京|tokyo|139.6917|35.6895|JP|city
大阪|osaka|135.5023|34.6937|JP|city
京都|kyoto|135.7681|35.0116|JP|city
福岛|fukushima|140.4676|37.7503|JP|city
札幌|sapporo|141.3545|43.0618|JP|city
小笠原|ogasawara|142.1917|27.0944|JP|region
冲绳|okinawa|127.6809|26.2124|JP|region
首尔|seoul|126.9780|37.5665|KR|city
釜山|busan|129.0756|35.1796|KR|city
平壤|pyongyang|125.7547|39.0392|KP|city
新德里|new delhi|77.2090|28.6139|IN|city
孟买|mumbai|72.8777|19.0760|IN|city
班加罗尔|bangalore|77.5946|12.9716|IN|city
克什米尔|kashmir|74.7973|34.0837|IN|region
伊斯兰堡|islamabad|73.0479|33.6844|PK|city
卡拉奇|karachi|67.0011|24.8607|PK|city
莫斯科|moscow|37.6173|55.7558|RU|city
圣彼得堡|saint petersburg|30.3351|59.9343|RU|city
基辅|kyiv|30.5234|50.4501|UA|city
哈尔科夫|kharkiv|36.2304|49.9935|UA|city
敖德萨|odesa|30.7326|46.4825|UA|city
顿巴斯|donbas|37.8000|48.3000|UA|region
顿涅茨克|donetsk|37.8029|48.0159|UA|city
克里米亚|crimea|34.1000|45.3000|UA|region
切尔诺贝利|chernobyl|30.2219|51.2763|UA|region
伦敦|london|-0.1276|51.5074|GB|city
曼彻斯特|manchester|-2.2426|53.4808|GB|city
巴黎|paris|2.3522|48.8566|FR|city
马赛|marseille|5.3698|43.2965|FR|city
柏林|berlin|13.4050|52.5200|DE|city
慕尼黑|munich|11.5820|48.1351|DE|city
法兰克福|frankfurt|8.6821|50.1109|DE|city
罗马|rome|12.4964|41.9028|IT|city
米兰|milan|9.1900|45.4642|IT|city
马德里|madrid|-3.7038|40.4168|ES|city
巴塞罗那|barcelona|2.1734|41.3851|ES|city
阿姆斯特丹|amsterdam|4.9041|52.3676|NL|city
布鲁塞尔|brussels|4.3517|50.8503|BE|city
维也纳|vienna|16.3738|48.2082|AT|city
日内瓦|geneva|6.1432|46.2044|CH|city
苏黎世|zurich|8.5417|47.3769|CH|city
斯德哥尔摩|stockholm|18.0686|59.3293|SE|city
奥斯陆|oslo|10.7522|59.9139|NO|city
赫尔辛基|helsinki|24.9384|60.1699|FI|city
哥本哈根|copenhagen|12.5683|55.6761|DK|city
华沙|warsaw|21.0122|52.2297|PL|city
布拉格|prague|14.4378|50.0755|CZ|city
布达佩斯|budapest|19.0402|47.4979|HU|city
雅典|athens|23.7275|37.9838|GR|city
安卡拉|ankara|32.8541|39.9334|TR|city
伊斯坦布尔|istanbul|28.9784|41.0082|TR|city
耶路撒冷|jerusalem|35.2137|31.7683|IL|city
特拉维夫|tel aviv|34.7818|32.0853|IL|city
加沙|gaza|34.4667|31.5000|PS|region
约旦河西岸|west bank|35.2694|31.9466|PS|region
拉马拉|ramallah|35.2038|31.9038|PS|city
大马士革|damascus|36.2913|33.5138|SY|city
阿勒颇|aleppo|37.1612|36.2021|SY|city
贝鲁特|beirut|35.5018|33.8938|LB|city
德黑兰|tehran|51.3890|35.6892|IR|city
巴格达|baghdad|44.3661|33.3152|IQ|city
摩苏尔|mosul|43.1189|36.3350|IQ|city
利雅得|riyadh|46.7219|24.7136|SA|city
迪拜|dubai|55.2708|25.2048|AE|city
多哈|doha|51.5310|25.2854|QA|city
萨那|sanaa|44.2067|15.3694|YE|city
亚丁|aden|45.0319|12.7855|YE|city
喀布尔|kabul|69.2075|34.5553|AF|city
开罗|cairo|31.2357|30.0444|EG|city
的黎波里|tripoli|13.1913|32.8872|LY|city
喀土穆|khartoum|32.5599|15.5007|SD|city
亚的斯亚贝巴|addis ababa|38.7578|9.0192|ET|city
内罗毕|nairobi|36.8219|-1.2921|KE|city
拉各斯|lagos|3.3792|6.5244|NG|city
阿布贾|abuja|7.3986|9.0765|NG|city
约翰内斯堡|johannesburg|28.0473|-26.2041|ZA|city
开普敦|cape town|18.4241|-33.9249|ZA|city
华盛顿|washington|-77.0369|38.9072|US|city
纽约|new york|-74.0060|40.7128|US|city
洛杉矶|los angeles|-118.2437|34.0522|US|city
旧金山|san francisco|-122.4194|37.7749|US|city
硅谷|silicon valley|-122.0300|37.3700|US|region
芝加哥|chicago|-87.6298|41.8781|US|city
休斯顿|houston|-95.3698|29.7604|US|city
西雅图|seattle|-122.3321|47.6062|US|city
波士顿|boston|-71.0589|42.3601|US|city
亚特兰大|atlanta|-84.3880|33.7490|US|city
底特律|detroit|-83.0458|42.3314|US|city
夏威夷|hawaii|-157.8583|21.3069|US|region
德克萨斯|texas|-97.5635|30.2672|US|region
加利福尼亚|california|-119.4179|36.7783|US|region
佛罗里达|florida|-81.5158|27.6648|US|region
渥太华|ottawa|-75.6972|45.4215|CA|city
多伦多|toronto|-79.3832|43.6532|CA|city
温哥华|vancouver|-123.1207|49.2827|CA|city
墨西哥城|mexico city|-99.1332|19.4326|MX|city
圣保罗|sao paulo|-46.6333|-23.5505|BR|city
里约热内卢|rio de janeiro|-43.1729|-22.9068|BR|city
巴西利亚|brasilia|-47.8825|-15.7942|BR|city
布宜诺斯艾利斯|buenos aires|-58.3816|-34.6037|AR|city
圣地亚哥|santiago|-70.6483|-33.4489|CL|city
利马|lima|-77.0428|-12.0464|PE|city
波哥大|bogota|-74.0721|4.7110|CO|city
加拉加斯|caracas|-66.9036|10.4806|VE|city
哈瓦那|havana|-82.3666|23.1136|CU|city
堪培拉|canberra|149.1300|-35.2809|AU|city
悉尼|sydney|151.2093|-33.8688|AU|city
墨尔本|melbourne|144.9631|-37.8136|AU|city
珀斯|perth|115.8605|-31.9505|AU|city
奥克兰|auckland|174.7633|-36.8485|NZ|city
惠灵顿|wellington|174.7787|-41.2924|NZ|city
新加坡城|downtown singapore|103.8198|1.3521|SG|city
吉隆坡|kuala lumpur|101.6869|3.1390|MY|city
曼谷|bangkok|100.5018|13.7563|TH|city
雅加达|jakarta|106.8456|-6.2088|ID|city
河内|hanoi|105.8342|21.0278|VN|city
胡志明市|ho chi minh city|106.6297|10.8231|VN|city
马尼拉|manila|120.9842|14.5995|PH|city
金边|phnom penh|104.9282|11.5564|KH|city
仰光|yangon|96.1951|16.8409|MM|city
科伦坡|colombo|79.8612|6.9271|LK|city
达卡|dhaka|90.4125|23.8103|BD|city
加德满都|kathmandu|85.3240|27.7172|NP|city
乌兰巴托|ulaanbaatar|106.9057|47.8864|MN|city
阿斯塔纳|astana|71.4304|51.1694|KZ|city
塔什干|tashkent|69.2401|41.2995|UZ|city
巴库|baku|49.8671|40.4093|AZ|city
第比利斯|tbilisi|44.8271|41.7151|GE|city
埃里温|yerevan|44.5152|40.1872|AM|city
台北101|taipei 101|121.5645|25.0339|TW|city
波斯湾|persian gulf|51.0000|27.0000|IR|region
霍尔木兹海峡|strait of hormuz|56.2500|26.5667|IR|region
红海|red sea|38.0000|20.0000|EG|region
苏伊士运河|suez canal|32.3000|30.5000|EG|region
巴拿马运河|panama canal|-79.6000|9.1000|PA|region
马六甲海峡|strait of malacca|100.5000|3.0000|MY|region
黑海|black sea|34.0000|43.0000|TR|region
南海|south china sea|114.0000|12.0000|CN|region
东海|east china sea|125.0000|29.0000|CN|region
台湾海峡|taiwan strait|119.5000|24.5000|TW|region
北极|arctic|0.0000|85.0000|NO|region
南极|antarctica|0.0000|-75.0000|AQ|region
喜马拉雅|himalaya|86.9250|27.9881|NP|region
科索沃|kosovo|21.1655|42.6629|XK|region
阿布哈兹|abkhazia|41.0000|43.0000|GE|region
纳卡|nagorno-karabakh|46.7500|39.8000|AZ|region
克什米尔地区|jammu and kashmir|74.7973|34.0837|IN|region
戈兰高地|golan heights|35.7500|32.9500|SY|region
扎波罗热|zaporizhzhia|35.1390|47.8388|UA|city
赫尔松|kherson|32.6178|46.6354|UA|city
明斯克|minsk|27.5615|53.9045|BY|city
贝尔格莱德|belgrade|20.4489|44.7866|RS|city
萨拉热窝|sarajevo|18.4131|43.8563|BA|city
布加勒斯特|bucharest|26.1025|44.4268|RO|city
索菲亚|sofia|23.3219|42.6977|BG|city
里斯本|lisbon|-9.1393|38.7223|PT|city
都柏林|dublin|-6.2603|53.3498|IE|city
雷克雅未克|reykjavik|-21.8277|64.1466|IS|city
那曲|nagqu|92.0512|31.4762|CN|region
日喀则|shigatse|88.8851|29.2675|CN|region
昌都|qamdo|97.1722|31.1406|CN|region
玉树|yushu|97.0085|33.0040|CN|region
汶川|wenchuan|103.5900|31.4800|CN|region
宜宾|yibin|104.6432|28.7518|CN|city
泸定|luding|102.2351|29.9141|CN|region
门头沟|mentougou|116.1014|39.9404|CN|region
丹东|dandong|124.3547|40.0008|CN|city
常山|changshan|118.5111|28.9000|CN|region
"""

# 会被误判为地名的组织/公司/品牌/球队后缀：命中即放弃该次匹配。
# 判断方式改为"紧跟在地名之后的片段是否**以**后缀开头"，避免像"美国总统制"
# 这种只是把某个字放在后面就被误伤的情况。
_ORG_SUFFIXES = (
    "银行", "公司", "集团", "证券", "基金", "药业", "制药", "汽车",
    "乳业", "重工", "钢铁", "煤业", "啤酒", "交易所", "研究院", "出版社", "合作社",
    "合作组织",
    # 球队与赛制：体育/娱乐标题里的地名多半指球队或赛制，不是事件发生地
    "队", "俱乐部", "后卫", "前锋", "中场", "门将", "主帅", "主教练",
    "联赛", "杯赛", "轮次", "赛区",
    # 以城市命名的俱乐部名（"辽宁铁人""大连英博""北京国安"…）。
    # 这是一份有意保守的常用清单：宁可漏掉个别俱乐部，也不把真实地点判掉。
    "铁人", "英博", "国安", "申花", "海港", "上港", "泰山", "蓉城", "三镇", "亚泰",
    "建业", "宏远", "首钢", "北控", "广厦", "稠州", "龙狮", "雄狮", "蓝鲸", "汾酒",
    "久事", "烈豹", "华南虎", "实德", "浐灞", "人和", "毅腾", "舜天",
)

# 地名后面紧跟"人数/年龄"这类描述时，地名通常是**人物籍贯或所属单位**，
# 而不是事件发生地（例如"上海12岁少年登顶乞力马扎罗"）。
_ROSTER = re.compile(r"^\s*\d{1,3}\s*(岁|年级|将|名|人|位|连胜|连败)")

# 明确的整词排除（子串会误判）
_EXCLUDE_PHRASES = (
    "上海合作组织", "上合组织", "上海证券交易所", "深圳证券交易所", "亚太经合组织",
    "新德里电视台", "北京银行", "云南白药", "贵州茅台", "青岛啤酒", "山西汾酒",
    # "瑞士轮/瑞士制"是赛制名称，与瑞士这个国家无关
    "瑞士轮", "瑞士制",
)


def _parse() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    for line in _RAW.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) != 6:
            continue
        zh, en, lon, lat, iso2, kind = parts
        try:
            lon_f, lat_f = float(lon), float(lat)
        except ValueError:
            continue
        key = (zh, en.lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append({"zh": zh, "en": en.lower(), "lon": lon_f, "lat": lat_f,
                        "iso2": iso2.upper(), "kind": kind,
                        "label": zh or en})
    # 最长优先：让 "中国台湾" 这类更长的词先于 "中国" 命中
    entries.sort(key=lambda e: len(e["zh"]), reverse=True)
    return entries


ENTRIES: List[Dict[str, Any]] = _parse()


def _is_organisation(text: str, end: int) -> bool:
    """地名后面紧跟组织/球队/人数描述时，不把它当作事件地点。"""
    tail = text[end:end + 6]
    if not tail:
        return False
    if _ROSTER.match(tail):
        return True
    return any(tail.startswith(suffix) for suffix in _ORG_SUFFIXES)


# ---------------------------------------------------------------- 体育语境
# 体育标题里的地名多半是**球队/赛事名**（广东队、北京男篮、辽宁铁人、"天津马拉松"），
# 不能一律当成事件发生地，但也不能一律丢弃——真实赛事确实发生在主办城市。
# 做法：体育语境下，只有出现明确的"在地线索"才保留该地名。
SPORTS_CUES = (
    "比赛", "球队", "球员", "联赛", "赛季", "夺冠", "卫冕", "世界杯", "奥运", "足球",
    "篮球", "网球", "乒乓", "国乒", "国羽", "女排", "男足", "国足", "马拉松", "决赛",
    "半决赛", "亚锦赛", "亚洲杯", "欧洲杯", "世预赛", "季后赛", "常规赛", "进球",
    "转会", "主帅", "主教练", "阵容", "首发", "MVP", "冠军", "亚军", "nba", "cba",
    "得分", "篮板", "助攻", "战绩", "积分榜", "赛程", "开赛", "收官",
    "首胜", "连败", "大胜", "惨败", "爆冷", "末节", "球迷",
)

# 明确的"事件发生在某地"线索：只有贴着地名的窗口里出现才算
LOCATIVE_AFTER = (
    "举行", "举办", "开幕", "开赛", "开跑", "开打", "召开", "发生", "爆发", "现场",
    "赛场", "主场", "位于", "落地", "落幕", "收官", "巡演", "巡展", "开幕", "办赛",
)
LOCATIVE_BEFORE = ("在", "于", "赴", "前往", "抵达", "来到", "现身", "发生在", "位于", "进博会")


def is_sports_context(text: str) -> bool:
    lowered = (text or "").lower()
    return any(cue.lower() in lowered for cue in SPORTS_CUES)


def has_locative_cue(text: str, span) -> bool:
    """地名前后是否出现"确实发生在这里"的线索（窗口很小，避免"在"字泛化误判）。"""
    raw = text or ""
    try:
        start, end = int(span[0]), int(span[1])
    except (TypeError, ValueError, IndexError):
        return False
    after = raw[end:end + 12]
    if any(after.startswith(cue) or cue in after for cue in LOCATIVE_AFTER):
        return True
    before = raw[max(0, start - 4):start]
    return any(before.endswith(cue) or cue in before for cue in LOCATIVE_BEFORE)


def _masked(text: str) -> str:
    masked = text
    for phrase in _EXCLUDE_PHRASES:
        masked = masked.replace(phrase, "\u0000" * len(phrase))
    return masked


def find(text: str, limit: int = 8) -> List[Dict[str, Any]]:
    """在标题里找出所有地名匹配（不重叠、最长优先）。返回带 span 的候选列表。"""
    raw = (text or "")
    if not raw:
        return []
    masked = _masked(raw)
    lowered = masked.lower()
    taken = [False] * len(raw)
    hits: List[Dict[str, Any]] = []

    for entry in ENTRIES:
        term_zh = entry["zh"]
        if term_zh and term_zh in masked:
            start = masked.find(term_zh)
            while start != -1:
                end = start + len(term_zh)
                if not any(taken[start:end]) and not _is_organisation(raw, end):
                    for index in range(start, end):
                        taken[index] = True
                    hits.append({"term": raw[start:end], "kind": entry["kind"],
                                 "iso2": entry["iso2"], "lat": entry["lat"], "lon": entry["lon"],
                                 "label": entry["label"], "span": [start, end], "lang": "zh"})
                    break
                start = masked.find(term_zh, start + 1)
        if entry["en"]:
            for match in re.finditer(r"(?<![a-z])" + re.escape(entry["en"]) + r"(?![a-z])", lowered):
                start, end = match.start(), match.end()
                if any(taken[start:end]):
                    continue
                for index in range(start, end):
                    taken[index] = True
                hits.append({"term": raw[start:end], "kind": entry["kind"],
                             "iso2": entry["iso2"], "lat": entry["lat"], "lon": entry["lon"],
                             "label": entry["label"], "span": [start, end], "lang": "en"})
                break

    hits.sort(key=lambda h: h["span"][0])
    return hits[:limit]


def metadata() -> Dict[str, Any]:
    return {
        "entries": len(ENTRIES),
        "kinds": {"city": sum(1 for e in ENTRIES if e["kind"] == CITY),
                  "region": sum(1 for e in ENTRIES if e["kind"] == REGION)},
        "source": "手工整理（坐标取官方首都/城市中心），国家名来自 Natural Earth",
        "license": "项目内自定义数据集；坐标事实不受版权保护",
    }