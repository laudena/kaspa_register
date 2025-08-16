from smartcard.System import readers

r = readers()
print("Readers:", r)

if r:
    connection = r[0].createConnection()
    connection.connect()
    print("Connected to reader:", r[0])
else:
    print("No smartcard readers found.")

