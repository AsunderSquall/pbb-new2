// QP二次扣分回归：连续低熟练度、真实名字、标量/四路及其他族。
#include "scoring_1124.hpp"
#include <cstdio>
#include <cstdlib>
#include <string>
static void check(bool ok,const char* msg){if(!ok){fprintf(stderr,"%s\n",msg);std::exit(1);}}
static ScoreResult evaluate(const std::string& name,const ScorePlan& p,const char* team="test"){
 Name n{};n.load_team(team);n.PRELEN=0;n.load_prefix(name.c_str(),(int)name.size());return score_full(name.c_str(),(int)name.size(),n,&p);
}
int main(){
 check(qp_shadow_deduction(0,200)==0,"zero illusion never deducted");
 check(qp_shadow_deduction(19.9,200)>0,"low illusion uses continuous correction");
 check(std::abs(qp_shadow_deduction(20,200)-2*qp_shadow_deduction(10,200))<1e-8,"linear in effective illusion");
 check(qp_shadow_deduction(60,300)>0&&qp_shadow_deduction(60.000001,300)>0,"no u60 cutoff");
 check(std::abs(qp_shadow_deduction(60,300)-60*(82.48156669219784-.24932235019037452*300+.0001942206736111687*300*300))<1e-8,"quadratic formula");
 ScorePlan p=plan_default();p.minv[M_QP]=1;
 const std::string name="03484286", potential_name="00112727",supplied=u8"帕秋莉 R6PIXzP",low=u8"旋风逐步测量杂费";
 auto original=evaluate(name,p),potential=evaluate(potential_name,p),user=evaluate(supplied,p,"Squall"),weak=evaluate(low,p,u8"昀澤");
 check(original.flag&&original.qp_reference_qp==4868&&original.sc[M_QP]==(int)(4868-original.qp_deduction)&&original.sc[M_PP]==6954,"surviving full score unchanged");
 check(potential.flag&&potential.sc[M_PJ]>0,"potential rescue control retained");
 check(user.flag&&user.illusion_effective>60&&user.qp_deduction>0&&user.qp_reference_qp==6361&&user.sc[M_QP]<5400,"user sample corrected QP");
 check(user.sc[M_FS]==4007,"FS retains original score");
 check(weak.flag&&weak.illusion_freq>60&&weak.illusion_effective<=60&&weak.qp_deduction>0,"effective rather than raw illusion");
 check(weak.qp_reference_qp==5434&&weak.sc[M_QP]==(int)(5434-weak.qp_deduction),"low effective sample raw QP preserved and corrected");
 // 单评分/any仍可由参与族或其潜力达标输出，不再硬排除整名。
 for(int m=0;m<M_N;++m){ScorePlan only{};only.comp[m]=true;only.order[0]=m;only.norder=1;only.minv[m]=1;only.pttv[m]=1;only.any_mode=true;
  auto r=evaluate(supplied,only,"Squall");check(r.flag&&score_output_keep(only,r,false)&&score_output_keep(only,r,true)==(r.sc[m]>=1||r.sc_max[m]>=1),"enabled family can still pass; no shadow gate");}
 // 混合lane与标量一致，覆盖有/无幻术、早退、存活；技能/属性/全评分/潜力均对照。
 for(int i=0;i<3000;i+=4){std::string candidates[4];Name lanes[4]{};const char* names[4];ScoreResult expected[4];
  for(int j=0;j<4;++j){char buf[20];snprintf(buf,sizeof(buf),"%08d",i+j);candidates[j]=buf;names[j]=candidates[j].c_str();lanes[j].load_team("test");lanes[j].PRELEN=0;lanes[j].load_prefix(names[j],8);expected[j]=evaluate(candidates[j],p);}
  Name::load_name_batch4(lanes,names,8);ScoreResult batch[4];score_full_loaded_batch4(names,8,lanes,batch,&p);
  for(int j=0;j<4;++j){auto&a=batch[j];auto&b=expected[j];check(a.flag==b.flag&&std::abs(a.qp_deduction-b.qp_deduction)<1e-8&&a.illusion_freq==b.illusion_freq&&a.illusion_effective==b.illusion_effective&&a.shadow_strength==b.shadow_strength,"scalar/batch classification");
   for(int m=0;m<M_N;++m)check(a.sc[m]==b.sc[m]&&a.sc_max[m]==b.sc_max[m],"scalar/batch full scores");}
 }
 Name lanes[4]{};const char* names[4]={supplied.c_str(),supplied.c_str(),supplied.c_str(),supplied.c_str()};
 for(auto&n:lanes){n.load_team("Squall");n.PRELEN=0;n.load_prefix(supplied.c_str(),(int)supplied.size());}
 Name::load_name_batch4(lanes,names,(int)supplied.size());ScoreResult batch[4];score_full_loaded_batch4(names,(int)supplied.size(),lanes,batch,&p);
 for(auto&r:batch)check(r.flag&&r.sc[M_QP]==user.sc[M_QP]&&r.sc[M_FS]==user.sc[M_FS]&&r.shadow_strength==user.shadow_strength&&r.illusion_effective==user.illusion_effective,"user sample batch corrected scores");
 // 极大QP/PP差值不再是输出过滤条件。
 ScoreResult arbitrary{};arbitrary.flag=true;arbitrary.sc[M_QP]=10000;arbitrary.sc[M_PP]=1;check(score_output_keep(p,arbitrary,true),"old QP-PP filter removed");
 printf("{\"name\":\"%s\",\"qp\":%d,\"pp\":%d,\"illusion_freq\":%d,\"potential_name\":\"%s\",\"pj\":%d,\"user_sample\":{\"qp\":%d,\"raw_qp\":%d,\"deduction\":%.12f,\"fs\":%d,\"illusion_freq\":%d,\"effective\":%.12f,\"s\":%.12f}}\n",name.c_str(),original.sc[M_QP],original.sc[M_PP],original.illusion_freq,potential_name.c_str(),potential.sc[M_PJ],user.sc[M_QP],user.qp_reference_qp,user.qp_deduction,user.sc[M_FS],user.illusion_freq,user.illusion_effective,user.shadow_strength);
}
