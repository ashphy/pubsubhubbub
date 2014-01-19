pubsubhubbub
============

PubSubHubbub protocol implementation, forked from https://code.google.com/p/pubsubhubbub/

## How to Run

### Hub
```
[pubsubhubbub] dev_appserver.py hub
```

### Publisher
```
[pubsubhubbub] dev_appserver.py --port=8081 --admin_port=8001 publisher
```

### Subscriber
```
[pubsubhubbub] dev_appserver.py --port=8082 --admin_port=8002 subscriber
```

## Dependeicies
- Python 2.7
- Google App Engine SDK for Python 1.8.9

## License

Apache License, Version 2.0 
