import hashlib

from petlibro_mcp.vendored.api import PetLibroAPI


def test_hash_password_is_md5_hex():
    # hash_password lives as a @staticmethod on PetLibroAPI in the upstream
    # source (not a module-level function) -- see task-2-report.md.
    assert PetLibroAPI.hash_password("secret") == hashlib.md5(b"secret").hexdigest()
