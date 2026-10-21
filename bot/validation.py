import re


def normalize_phone(value: str) -> str:
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r'\+?[0-9 ()-]+', value):
        raise ValueError('Введи телефон цифрами, например +79991234567')
    cleaned = re.sub(r'[ ()-]', '', value)
    if cleaned.startswith('+'):
        if not re.fullmatch(r'\+[1-9][0-9]{6,14}', cleaned):
            raise ValueError('Неверный международный номер')
        return cleaned
    if len(cleaned) == 11 and cleaned[0] in '78':
        return '+7' + cleaned[1:]
    raise ValueError('Используй 8XXXXXXXXXX, 7XXXXXXXXXX или международный номер с +')


def normalize_name(value: str) -> str:
    value = ' '.join(value.split())
    if not 2 <= len(value) <= 80 or not any(c.isalpha() for c in value):
        raise ValueError('Введи имя от 2 до 80 символов')
    return value
