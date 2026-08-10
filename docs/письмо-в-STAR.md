# Письмо в STAR за доступом к вакансиям Jobnet (шаг 7 плана)

Готовил ассистент 10.08.2026. **Отправляет Иван — сам, со своего адреса.**
Ассистент письма не отправляет и от имени Ивана ни с кем не переписывается.

---

## Что изменилось по сравнению с планом

В плане (08.08.2026) сервис назван **JobAdService, WSDL v3**. По документации
STAR его заменили: сейчас это **JobannonceService** («JobAnnonceService
erstatter den tidligere JobAd Service»), у него есть версии 1 и 2, и с релиза
2023-3 он доступен как Swagger-эндпоинт на `virksomhedsindsats.bm.dk`. Старое
название в переписке лучше не использовать — попросим «webservice til import og
eksport af jobannoncer», как STAR называет его на своей странице.

Публично сервис не открыт: Swagger по прямой ссылке отвечает 500 без доступа.
Значит путь один — соглашение.

## Что мы просим (важно не перепутать роль)

У сервиса **две роли**, и нам нужна вторая:

| Роль | Смысл | Нужна нам? |
|---|---|---|
| Экспорт в Jobnet | публиковать СВОИ вакансии на jobnet.dk | нет |
| **Импорт из Jobnet** | **забирать опубликованные вакансии для показа на своём портале** | **да** |

Формально это «importere annoncer til udstilling på egen jobportal eller
hjemmeside». Мы читаем чужие вакансии, ничего не публикуем.

## Что будет дальше (чтобы Иван знал заранее)

1. Пишем на `spoc@star.dk`, указываем роль и зачем.
2. STAR присылает **tilslutningsaftale** (соглашение о подключении). Его нужно
   заполнить и подписать — **до подписи тестовую среду не дают**.
3. После подписи — доступ к тестовой среде, потом к проду.
4. Сам сервис бесплатный. Новому клиенту дают **5 часов бесплатной поддержки**
   ИТ-поставщика STAR; сверх этого — почасовая оплата напрямую с поставщиком.
   То есть счёт может появиться только если мы сами потратим много часов чужой
   поддержки; за доступ и за данные не платим.

Юридически это единственная законная дорога к национальной базе: Jobindex мы
сознательно не трогаем (нет официального API, robots.txt запрещает страницы
поиска, а для платного сервиса это риск).

---

## Текст письма (датский) — можно отправлять как есть

> **Emne:** Adgang til webservice til import af jobannoncer (JobannonceService)
>
> Kære Styrelsen for Arbejdsmarked og Rekruttering
>
> Jeg vil gerne ansøge om adgang til jeres webservice til import og eksport af
> jobannoncer — i rollen, hvor man **importerer offentliggjorte jobannoncer fra
> Jobnet til visning på egen jobportal**. Vi ønsker ikke at publicere annoncer
> på Jobnet.
>
> **Om os.** Jeg udvikler WexFlow — et lille dansk-orienteret værktøj, der
> hjælper jobsøgende (primært udlændinge i Danmark, herunder ukrainere) med at
> finde relevante stillinger og udfylde ansøgningsskemaer. Værktøjet henter i
> dag stillinger fra virksomhedernes egne officielle API'er og viser dem samlet
> for brugeren. Jobnet ville give brugerne et langt mere fuldstændigt billede af
> det danske arbejdsmarked.
>
> **Hvad vi har brug for.**
> - rollen "import" (læse offentliggjorte jobannoncer), ikke "eksport";
> - adgang til testmiljø og efterfølgende produktion;
> - vejledning til hvilken version af servicen vi skal integrere mod
>   (JobannonceService v1 eller v2), og hvilken autentifikation der kræves
>   (certifikat/OCES eller andet).
>
> **Spørgsmål, jeg gerne vil have afklaret inden underskrift:**
> 1. Er der krav til minimumsvolumen eller til, at serviceaftageren driver en
>    offentligt tilgængelig jobportal?
> 2. Er der begrænsninger på, hvor ofte vi må kalde servicen, og hvor mange
>    annoncer vi må hente og opbevare lokalt?
> 3. Hvilke betingelser gælder for visning af annoncerne over for slutbrugeren
>    (kildeangivelse, link tilbage til Jobnet, opdaterings- og sletteregler)?
> 4. Må annoncernes tekst vises oversat til brugerens eget sprog, når kilden
>    tydeligt fremgår?
> 5. Hvad er den forventede tidshorisont fra underskrevet tilslutningsaftale til
>    adgang til testmiljøet?
>
> Send gerne tilslutningsaftalen, så udfylder og underskriver jeg den.
>
> På forhånd tak.
>
> Med venlig hilsen
> Ivan
> [телефон] · [email]

---

## То же по-русски (чтобы Иван понимал, что отправляет)

Тема: доступ к веб-сервису импорта вакансий (JobannonceService).

Смысл: прошу доступ в роли «импорт» — читать опубликованные на Jobnet вакансии и
показывать их на своём портале; публиковать свои вакансии не собираемся.
Коротко о нас: WexFlow помогает соискателям (в первую очередь иностранцам в
Дании, включая украинцев) находить подходящие вакансии и заполнять анкеты;
сейчас берём вакансии из официальных API самих работодателей.

Спрашиваем заранее пять вещей: (1) есть ли требования к объёму или к тому,
чтобы у нас был публичный портал; (2) ограничения на частоту запросов и на
локальное хранение; (3) условия показа вакансий пользователю (указание
источника, ссылка на Jobnet, правила обновления и удаления); (4) можно ли
показывать перевод текста вакансии при явном указании источника; (5) сколько
времени пройдёт от подписи до тестовой среды.

Просим прислать tilslutningsaftale.

> ⚠️ **Перед отправкой** подставь телефон и email вместо `[телефон]` и
> `[email]`. Больше в письме ничего личного нет: ни адреса, ни CV, ни данных
> пользователей.

## Вопросы 3 и 4 — не формальность

Ответ STAR на них определит, что мы вообще имеем право делать с текстом
вакансии: перевод на русский (`translate_worker.py`) и показ в Telegram — это
уже воспроизведение чужого объявления. Лучше получить письменное «да», чем
потом убирать функцию.

## Источники

- [STAR: Webservice til import og eksport af jobannoncer](https://star.dk/digital-service/saadan-arbejder-vi-med-it-i-styrelsen/oversigt-over-digitale-platforme-for-eksterne-brugere/styrelsen-for-arbejdsmarked-og-rekrutterings-webservices-og-wiki/webservice-til-import-og-eksport-af-jobannoncer)
- [STAR: Jobnet webservice](https://star.dk/digital-service/saadan-arbejder-vi-med-it-i-styrelsen/oversigt-over-digitale-platforme-for-eksterne-brugere/styrelsen-for-arbejdsmarked-og-rekrutterings-webservices-og-wiki/jobnet-webservice)
- [STARWIKI: JobannonceService (заменяет JobAdService)](https://starwiki.atlassian.net/wiki/spaces/FYS/pages/4023386657/JobannonceService+Velkomstside)
- [STARWIKI: вопросы и ответы по JobAnnonceService V2](https://starwiki.atlassian.net/wiki/spaces/FYS/pages/4380983425)

Технические детали (методы `SearchJob` + `GetJob`, версии сервиса, способ
аутентификации) в вики видны только частично — страницы отдаются урывками.
Дочитывать их имеет смысл уже с доступом, после ответа STAR.
