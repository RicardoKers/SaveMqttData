#!/usr/bin/env python3

import sys
from argon2 import PasswordHasher

def main():
    if len(sys.argv) != 2:
        print("Usage: GenerateHash <password>")
        print("Example: GenerateHash mypasskey")
        sys.exit(1)

    password = sys.argv[1]

    # Instantiate PasswordHasher with Argon2's default parameters
    # (you can adjust as needed, for example, time_cost, memory_cost, parallelism, etc.)
    ph = PasswordHasher()

    # Generate the hash (internally Argon2 generates and stores the salt)
    # The result will be a string containing all the necessary data (version, salt, etc.)
    generated_hash = ph.hash(password)

    # To extract information from the hash, we can simply display the complete hash.
    # Argon2-cffi stores the salt and the hash combined in Base64 after the header.
    # In many cases, this "generated_hash" can be stored as Argon2's "hash + salt".
    print("Copy and paste the hash below into your config.json.")
    print("You can store it as the value of the 'passkeyHash' field.")
    print("")
    print("====================================")
    print("Argon2 Hash (with embedded salt):")
    print(generated_hash)
    print("====================================")

if __name__ == "__main__":
    main()
