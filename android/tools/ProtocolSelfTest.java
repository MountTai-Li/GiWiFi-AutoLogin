import com.mounttai.giwifi.Aes128;
import com.mounttai.giwifi.GiwifiClient;

import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;

/** 桌面自测：验证 Java 版加密/编码/解析与电脑版 Python 实现逐字节一致 */
public class ProtocolSelfTest {
    public static void main(String[] args) throws Exception {
        // 1) AES 三个样本
        System.out.println("AES1=" + Aes128.encrypt(
                "name=19819524363&password=abc123&iv=abcdefghijklmnop", "abcdefghijklmnop"));
        System.out.println("AES2=" + Aes128.encrypt(
                "name=测试账号&password=密码123&sign=abc", "1234567890abcdef"));
        // 边界：正好 16 字节（验证「已对齐不补块」）
        System.out.println("AES3=" + Aes128.encrypt("1234567890abcdef", "1234567890abcdef"));

        // 2) encodeURIComponent 语义
        System.out.println("JQ1=" + GiwifiClient.jqEnc("中文 a+b=c&d/e?f=g h'()!~*"));

        // 3) 真实登录页解析
        String html = new String(Files.readAllBytes(Paths.get(args[0])), "UTF-8");
        List<String[]> fields = GiwifiClient.parseLoginFields(html);
        System.out.println("FIELDS_START");
        for (String[] f : fields) {
            System.out.println(f[0] + "=" + f[1]);
        }
        System.out.println("FIELDS_END");

        // 4) 完整序列化 + 加密（用假账号）
        StringBuilder plain = new StringBuilder();
        String iv = null;
        for (String[] f : fields) {
            String k = f[0], v = f[1];
            if (k.equals("name")) v = "19819524363";
            else if (k.equals("password")) v = "test-pass-123";
            if (k.equals("iv")) iv = v;
            if (plain.length() > 0) plain.append('&');
            plain.append(GiwifiClient.jqEnc(k)).append('=').append(GiwifiClient.jqEnc(v));
        }
        System.out.println("PLAIN=" + plain);
        System.out.println("SERIALIZED_AES=" + Aes128.encrypt(plain.toString(), iv));
    }
}
