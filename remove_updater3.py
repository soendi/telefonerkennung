with open('telefonerkennung.py', 'rb') as f:
    content = f.read()

# Remove UPDATER_SCRIPT section (from "UPDATER_SCRIPT = r'''" to just before "def setup_logging():")
start = content.find(b"UPDATER_SCRIPT = r'''")
end = content.find(b"\r\n\r\ndef setup_logging():")  # Two newlines before def setup_logging

if start >= 0 and end >= 0:
    # Include the newlines before def setup_logging in the kept portion
    content = content[:start] + content[end:]
    with open('telefonerkennung.py', 'wb') as f:
        f.write(content)
    print("UPDATER_SCRIPT removed successfully")
else:
    print("Start or end not found")