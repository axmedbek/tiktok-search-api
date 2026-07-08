

class MetasecBaseException(Exception):
    pass


class InvalidEncryptionKey(MetasecBaseException):
    pass


class InvalidURL(MetasecBaseException):
    pass


class UnsupportedDynVersion(MetasecBaseException):
    pass
