"""Умный поиск: расширяет русский (и английский) запрос датскими синонимами,
т.к. вакансии Salling написаны по-датски."""
import re

# русское/англ. слово -> список датских терминов, которые ищем в title/description
RU_DA = {
    "кассир": ["kasse", "kassemedarbejder", "kasseassistent"],
    "касса": ["kasse"],
    "пекарь": ["bager"],
    "пекарня": ["bageri"],
    "мясник": ["slagter"],
    "продавец": ["salgsassistent", "sælger", "butiksassistent", "salg"],
    "продажи": ["salg", "salgsassistent"],
    "склад": ["lager", "warehouse", "logistik"],
    "логистика": ["logistik", "supply chain"],
    "водитель": ["chauffør", "kører"],
    "уборщик": ["rengøring", "rengøringsassistent"],
    "уборка": ["rengøring"],
    "менеджер": ["leder", "chef", "manager"],
    "руководитель": ["leder", "chef", "ansvarlig"],
    "директор": ["direktør", "chef"],
    "студент": ["student", "studentermedhjælper", "studie"],
    "стажер": ["elev", "lærling", "praktik"],
    "стажёр": ["elev", "lærling", "praktik"],
    "ученик": ["elev", "lærling"],
    "практика": ["praktik", "praktikant"],
    "помощник": ["medhjælper", "assistent"],
    "ассистент": ["assistent"],
    "сервис": ["service", "servicemedarbejder"],
    "обслуживание": ["service", "kundeservice"],
    "клиент": ["kunde", "kundeservice"],
    "продукты": ["fødevarer", "dagligvarer", "frugt", "grønt"],
    "фрукты": ["frugt"],
    "овощи": ["grønt", "grøntsager"],
    "ресторан": ["restaurant"],
    "кафе": ["café", "cafe"],
    "повар": ["kok", "køkken"],
    "кухня": ["køkken"],
    "электроника": ["elektronik"],
    "бухгалтер": ["økonomi", "bogholder", "regnskab"],
    "финансы": ["økonomi", "finans"],
    "маркетинг": ["marketing"],
    "ночной": ["nat", "natten"],
    "ночь": ["nat"],
    "утренний": ["morgen", "morgenopfylder"],
    "выкладка": ["opfylder", "varepåfyldning"],
    "текстиль": ["tekstil"],
    "одежда": ["tøj", "tekstil"],
    "охрана": ["sikkerhed", "vagt"],
    "ит": ["it", "udvikler", "developer"],
    "разработчик": ["udvikler", "developer"],
    "персонал": ["hr", "personale"],
    "полный": ["fuldtid"],
    "частичная": ["deltid"],
    "подработка": ["deltid", "ungarbejder"],
}


def has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[а-яё]", s, re.I))


def expand(query: str) -> list[str]:
    """Возвращает список терминов для поиска: сам запрос + датские синонимы."""
    q = (query or "").strip()
    if not q:
        return []
    terms = []
    low = q.lower()
    # пословный разбор для синонимов
    matched = False
    for word in re.split(r"[\s,]+", low):
        for ru, das in RU_DA.items():
            if word == ru or (len(word) >= 4 and word in ru):
                terms.extend(das)
                matched = True
    # если кириллица и ничего не нашли — оставим исходный (вернёт пусто, но без ошибки)
    if not has_cyrillic(q):
        terms.append(q)  # латиница ищется как есть
    elif not matched:
        terms.append(q)
    # уникализируем, сохраняя порядок
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out
