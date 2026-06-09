"""Установка офлайн-переводчика датский -> русский для Argos Translate.

Запуск:
  pip install argostranslate
  python install_offline_translator.py
"""


def main():
    try:
        import argostranslate.package as package
        import argostranslate.translate as translate
    except ImportError:
        raise SystemExit(
            "Сначала установи пакет: pip install argostranslate"
        )

    if translate.get_translation_from_codes("da", "ru"):
        print("Модель da -> ru уже установлена.")
        return

    print("Обновляю список моделей Argos...")
    package.update_package_index()
    available = package.get_available_packages()
    match = next((p for p in available if p.from_code == "da" and p.to_code == "ru"), None)
    if not match:
        raise SystemExit("Не нашёл модель da -> ru в индексе Argos.")

    print(f"Скачиваю модель: {match}")
    path = match.download()
    print("Устанавливаю модель...")
    package.install_from_path(path)
    print("Готово: офлайн-перевод da -> ru установлен.")


if __name__ == "__main__":
    main()
