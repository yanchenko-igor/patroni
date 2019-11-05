#!/usr/bin/env python
from patroni import main
from patroni.exceptions import ConfigParseException


if __name__ == '__main__':
    try:
        main()
    except ConfigParseException as e:
        print(e)
