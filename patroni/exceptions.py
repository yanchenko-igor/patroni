import yaml

class PatroniException(Exception):

    """Parent class for all kind of exceptions related to selected distributed configuration store"""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        """
        >>> str(PatroniException('foo'))
        "'foo'"
        """
        return repr(self.value)


class PostgresException(PatroniException):
    pass


class DCSError(PatroniException):
    pass


class PostgresConnectionException(PostgresException):
    pass


class WatchdogError(PatroniException):
    pass

class PatroniConfigError(PatroniException):
    pass

class ConfigParseException(PatroniException):
    def __init__(self, config, msg):
        self.config = config
        self.msg = msg

    def __str__(self):
        return "\n{1}\n\n{0}".format(yaml.dump(self.config.copy()), self.msg)
