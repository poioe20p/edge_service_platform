import eventlet

# Must run before os_ken is imported, or os_ken's RLock use is not greened.
eventlet.monkey_patch()

from os_ken.cmd.manager import main
main()
