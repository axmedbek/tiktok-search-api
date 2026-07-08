class RC4(object):
    def __init__(self, key: bytes):
        self.table = list(range(256))
        self.index1 = 0
        self.index2 = 0

        self.key = key
        self.cipher = []
        self.secret = None

        self.key_length = len(key)

    def init(self) -> None:
        for i in range(256):
            self.index1 = (
                self.index1 + self.table[i] + self.key[i % self.key_length]
            ) % 256
            self.table[i], self.table[self.index1] = (
                self.table[self.index1],
                self.table[i],
            )
        self.index1 = 0

    def encrypt(self, secret: bytes) -> bytes:
        self.cipher = []
        self.secret = secret

        for car in self.secret:
            self.index1 = (self.index1 + 1) % 256
            self.index2 = (self.table[self.index1] + self.index2) % 256
            self.table[self.index1], self.table[self.index2] = (
                self.table[self.index2],
                self.table[self.index1],
            )
            self.cipher.append(
                car
                ^ self.table[(self.table[self.index1] + self.table[self.index2]) % 256]
            )
        self.cipher = bytes(self.cipher)

        return self.cipher