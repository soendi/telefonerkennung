with open('telefonerkennung.py', 'rb') as f:
    content = f.read()
content = content.replace(b'APP_VERSION = "3.2.6"', b'APP_VERSION = "3.2.7"')
with open('telefonerkennung.py', 'wb') as f:
    f.write(content)
print('Done')