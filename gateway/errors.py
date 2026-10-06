from dataclasses import dataclass


@dataclass
class GatewayError(Exception):
    status: int
    code: str
    message: str

    def __str__(self):
        return self.code

    def as_dict(self):
        return {"error": {"message": self.message, "type": self.code, "param": None, "code": self.code}}


def invalid(message="Request does not match the supported text contract."):
    return GatewayError(400, "invalid_request", message)
