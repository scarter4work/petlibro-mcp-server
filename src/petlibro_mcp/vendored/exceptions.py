class PetLibroError(Exception):
    """Base error for the standalone PetLibro API client."""


class PetLibroAPIError(PetLibroError):
    "Basic API error"


class PetLibroCannotConnect(PetLibroAPIError):
    """Error to indicate we cannot connect."""


class PetLibroInvalidAuth(PetLibroAPIError):
    """Error to indicate there is invalid auth."""
