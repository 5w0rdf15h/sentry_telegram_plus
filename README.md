форк https://github.com/butorov/sentry-telegram

# Sentry Telegram Plus

Позволяет управлять тем, в какие каналы будут падать сообщения. Поддерживает треды.

Установка / переустановка старой версии (это неправильный подход, подходит для отладки), правильный подход через sentry/enhance-image.sh https://develop.sentry.dev/self-hosted/configuration/

```
pip uninstall sentry-telegram-plus
pip install https://github.com/path/to/zip/archive/master.zip   
```

либо установка из скомпилированного wheel-файла

`scp sentry_telegram_plus-0.6.1-py3-none-any.whl USERNAME@sentry.hd:` - копируем файл на сервер по SFTP

на сервере:
находим id контейнера куда будет установлен плагин (для примера тут он `b0eeb2a4b2ab`)

`docker ps |grep web`
 

```
docker cp sentry_telegram_plus-0.6.1-py3-none-any.whl b0eeb2a4b2ab:/sentry_telegram_plus-0.6.1-py3-none-any.whl
Successfully copied 17.9kB to b0eeb2a4b2ab:/sentry_telegram_plus-0.6.1-py3-none-any.whl
```

идем в контейнер `docker exec -ti b0eeb2a4b2ab bash`
ставим плагин `pip install sentry_telegram_plus-0.6.1-py3-none-any.whl`

выходим из контейнера и рестартим его 

`docker restart sentry-self-hosted-web-1` 

Рестарт контейнера занимает 10-20 секунд. Переустановка Sentry при описанном выше сценарии не требуется.

для того чтобы получить айдишники каналов, можно открыть (в т.ч. в браузере) так (GET запрос):
https://api.telegram.org/bot271488016:AAHDAGBHoGmL_xj0gMR1cX9GIuW8buWz141/getUpdates

где `AAHDAGBHoGmL_xj0gMR1cX9GIuW8buWz141` - токен (надо подставить свой правильный)

##### Для того чтобы узнать ID треда в канале:

* в треде пишем сообщение (через обычный клиент), жмем правой кнопкой на отправленное сообщение, в появившемся меню копируем ссылку https://t.me/fadssafd3242/2/23 (цифра 2 после / - это и есть тред)

Примеры конфигов с фильтрами - https://jira.hellodoc.team/browse/HFA-5051

#### История версий
* 0.6.2 - добавлена возможность делать в фильтрах логику AND, OR
* 0.6.1 - первая версия, форк основного плагина. Добавлена возможность оправки в несколько каналов с разными настройками / фильтрами.
