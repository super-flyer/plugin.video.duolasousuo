import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/** 单文件 Java Spider Host（source-file mode 可编译运行）。
 *  反射调用 com.github.catvod.spider.<Class> 的 Spider 契约，结果以 JSON 字符串输出。
 *  不 import 任何第三方库，避免编译期依赖；Spider 自身依赖由 -cp 提供。
 */
public class SpiderHostRunner {
    private static String escape(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.toString();
    }

    private static String invoke(Object target, String name, Class<?>[] types, Object[] values) throws Exception {
        Method m = target.getClass().getMethod(name, types);
        Object result = m.invoke(target, values);
        return result == null ? "" : String.valueOf(result);
    }

    private static String invokeAny(Object target, String name, Class<?>[][] typeSets, Object[][] valueSets) throws Exception {
        Exception last = null;
        for (int i = 0; i < typeSets.length; i++) {
            try {
                return invoke(target, name, typeSets[i], valueSets[i]);
            } catch (NoSuchMethodException e) {
                last = e;
            }
        }
        throw last == null ? new NoSuchMethodException(name) : last;
    }

    public static void main(String[] args) {
        String data = "{}";
        boolean ok = true;
        String error = "";
        try {
            String action = args[0];
            String simple = args[1];
            String ext = args.length > 2 ? args[2] : "";
            Class<?> spiderClass = Class.forName("com.github.catvod.spider." + simple);
            Object spider = spiderClass.getDeclaredConstructor().newInstance();
            boolean inited = false;
            try {
                spiderClass.getMethod("init", String.class).invoke(spider, ext);
                inited = true;
            } catch (NoSuchMethodException ignored) {
            } catch (Exception e) {
                System.err.println("init(String) failed: " + e);
            }
            if (!inited) {
                try {
                    spiderClass.getMethod("init").invoke(spider);
                } catch (NoSuchMethodException ignored) {
                } catch (Exception e) {
                    System.err.println("init() failed: " + e);
                }
            }

            String out = "";
            if ("search".equals(action)) {
                String key = args[3];
                out = invokeAny(spider, "searchContent",
                        new Class<?>[][]{{String.class, boolean.class},
                                {String.class, boolean.class, String.class},
                                {String.class}},
                        new Object[][]{{key, true}, {key, true, "1"}, {key}});
            } else if ("detail".equals(action)) {
                out = invoke(spider, "detailContent", new Class<?>[]{List.class},
                        new Object[]{Arrays.asList(args[3])});
            } else if ("play".equals(action)) {
                out = invoke(spider, "playerContent",
                        new Class<?>[]{String.class, String.class, List.class},
                        new Object[]{args[3], args[4], Collections.emptyList()});
            } else if ("home".equals(action)) {
                out = invoke(spider, "homeVideoContent", new Class<?>[]{}, new Object[]{});
            } else {
                throw new IllegalArgumentException("unknown action: " + action);
            }
            data = (out == null || out.trim().isEmpty()) ? "{}" : out.trim();
        } catch (Throwable t) {
            ok = false;
            error = t.toString();
            data = "{}";
        }
        System.out.println("{\"ok\":" + ok + ",\"data\":\"" + escape(data)
                + "\",\"error\":\"" + escape(error) + "\"}");
    }
}
