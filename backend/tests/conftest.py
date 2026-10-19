import os

# The Open House (open_house.py) follows the real clock. Keep it off for the test run so the paywall tests mean the
# same thing on any day; test_open_house.py turns it on itself.
os.environ["OPEN_HOUSE"] = "off"
