# Лендинг kervax.ru

Статический сайт: две языковые версии (`site/index.html` — русская,
`site/en/index.html` — английская), общий `style.css`, крошечный `lb.js` для
лайтбокса и кнопки «копировать». Ни сборки, ни зависимостей.

Скриншоты в `site/shots/` — те же кадры, что в README, пересобранные в webp
(обычный размер плюс `@2x` для лайтбокса):

```bash
python docs/make-shots.py          # пересобрать кадры панели
python landing/build-shots.py      # перегнать их в webp для сайта
```

## Как развёрнут

Сейчас сайт живёт на хостинге под HestiaCP, где nginx раздаёт статику из
`/home/ms-web/web/kervax.ru/public_html`. Домен и сертификат заводятся её
средствами:

```bash
v-add-web-domain <user> kervax.ru
v-add-letsencrypt-domain <user> kervax.ru
```

Выкладка — обычный tar по ssh:

```bash
cd landing/site && tar -czf - . | ssh <host> 'sudo tar -xzf - -C /home/<user>/web/kervax.ru/public_html'
```

`robots.txt` создаёт сама панель хостинга, поэтому в репозитории его нет.

## Если сайт переедет на сервер с Docker

Тогда проще поднять его рядом с панелью — nginx с примонтированной `site/` и
метками caddy-docker-proxy, как это сделано у остальных сервисов. Отдельным
стеком, а не сервисом внутри compose панели: лендинг не должен переживать её
пересборку, а панель — зависеть от лендинга.
