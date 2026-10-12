with open('telefonerkennung.py', 'rb') as f:
    content = f.read()

# Fix the indentation of def end_test_call
old = b'\r\n        logger.info("TEST: after(15000, end_test_call) gesetzt")\r\n\r\ndef end_test_call(self):'
new = b'\r\n        logger.info("TEST: after(15000, end_test_call) gesetzt")\r\n\r\n    def end_test_call(self):'

if old in content:
    content = content.replace(old, new)
    with open('telefonerkennung.py', 'wb') as f:
        f.write(content)
    print("Fixed")
else:
    print("Pattern not found")